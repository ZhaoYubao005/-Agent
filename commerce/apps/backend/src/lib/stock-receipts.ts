import { createHash, timingSafeEqual } from "node:crypto"
import type { MedusaContainer } from "@medusajs/framework"
import type { Knex } from "@medusajs/framework/mikro-orm/knex"
import { BigNumber, ContainerRegistrationKeys, MathBN, Modules } from "@medusajs/framework/utils"
import { z } from "@medusajs/framework/zod"

export const stockReceiptJournal = "ecomops_stock_receipt"

const identifier = z.string().min(1).max(255).refine((value) => !/[^A-Za-z0-9_-]/.test(value))
const idempotencyKey = z.string().min(1).max(255).refine((value) => !/[^A-Za-z0-9_:-]/.test(value))

export const stockReceiptInput = z.strictObject({
  idempotency_key: idempotencyKey,
  inventory_item_id: identifier,
  location_id: identifier,
  quantity: z.number().int().min(1).max(10000),
  expected_stocked: z.number().int(),
  expected_reserved: z.number().int().nonnegative(),
}).refine((input) => {
  const target = input.expected_stocked + input.quantity
  return Number.isSafeInteger(target) && Number.isSafeInteger(target - input.expected_reserved)
})

type ReceiptInput = z.infer<typeof stockReceiptInput>

type ReceiptOutcome =
  | { status: 200; body: {
    inventory_item_id: string
    location_id: string
    before_stocked: number
    after_stocked: number
    available_quantity: number
  } }
  | { status: 404 | 409 | 422; body: { code: string } }

type JournalEntry = {
  idempotency_key: string
  fingerprint: string
  outcome: ReceiptOutcome | null
}

type InventoryLevel = {
  id: string
  inventory_item_id: string
  location_id: string
  stocked_quantity: string
  reserved_quantity: string
  raw_stocked_quantity: unknown
  raw_reserved_quantity: unknown
  updated_at: Date
}

const levelFields = [
  "id", "stocked_quantity", "reserved_quantity", "raw_stocked_quantity", "raw_reserved_quantity",
] as const

export function hasReceiptToken(provided: unknown): boolean {
  const configured = process.env.ECOMOPS_STOCK_CAS_TOKEN
  return process.env.ECOMOPS_STOCK_CAS_MODE === "controlled"
    && typeof configured === "string"
    && configured.length >= 32 && configured.length <= 256 && !/[^\x21-\x7e]/.test(configured)
    && typeof provided === "string" && provided.length === configured.length
    && timingSafeEqual(
      createHash("sha256").update(configured).digest(),
      createHash("sha256").update(provided).digest(),
    )
}

export function receiptInScope(input: ReceiptInput): boolean {
  const scope = (process.env.ECOMOPS_STOCK_CAS_SCOPE ?? "").split(",")
  return scope.every((entry) => {
    const parts = entry.split(":")
    return parts.length === 2 && parts.every((part) => identifier.safeParse(part).success)
  }) && scope.includes(`${input.inventory_item_id}:${input.location_id}`)
}

function readQuantity(numeric: string, raw: unknown): number | undefined {
  if (!raw || typeof raw !== "object" || !("value" in raw) || typeof raw.value !== "string") {
    return undefined
  }
  const quantity = Number(numeric)
  if (Number.isSafeInteger(quantity) && MathBN.eq(quantity, numeric) && MathBN.eq(quantity, raw.value)) {
    return quantity
  }
  return undefined
}

async function applyReceipt(tx: Knex.Transaction, input: ReceiptInput): Promise<ReceiptOutcome> {
  // v2.22.3 stores numeric and raw JSONB quantities. Availability uses the raw columns.
  const level = await tx("inventory_level as level")
    .innerJoin("inventory_item as item", "level.inventory_item_id", "item.id")
    .where({ "level.inventory_item_id": input.inventory_item_id, "level.location_id": input.location_id })
    .whereNull("level.deleted_at")
    .whereNull("item.deleted_at")
    .forUpdate("level", "item")
    .first<Pick<InventoryLevel, typeof levelFields[number]> | undefined>(
      ...levelFields.map((field) => `level.${field}`),
    )
  if (!level) {
    return { status: 404, body: { code: "inventory_level_not_found" } }
  }

  const stocked = readQuantity(level.stocked_quantity, level.raw_stocked_quantity)
  const reserved = readQuantity(level.reserved_quantity, level.raw_reserved_quantity)
  if (stocked === undefined || reserved === undefined || reserved < 0) {
    return { status: 422, body: { code: "invalid_inventory_snapshot" } }
  }
  if (stocked !== input.expected_stocked || reserved !== input.expected_reserved) {
    return { status: 409, body: { code: "stale_inventory_snapshot" } }
  }

  const target = stocked + input.quantity
  const rowsUpdated = await tx<InventoryLevel>("inventory_level")
    .where({ id: level.id })
    .update({
      stocked_quantity: String(target),
      raw_stocked_quantity: JSON.stringify({ value: String(target), precision: BigNumber.DEFAULT_PRECISION }),
      updated_at: tx.fn.now(),
    })
    .returning("id")
  const verified = await tx<InventoryLevel>("inventory_level")
    .where({ id: level.id, inventory_item_id: input.inventory_item_id, location_id: input.location_id })
    .whereNull("deleted_at")
    .first(...levelFields)
  if (rowsUpdated.length !== 1 || !verified
      || readQuantity(verified.stocked_quantity, verified.raw_stocked_quantity) !== target
      || readQuantity(verified.reserved_quantity, verified.raw_reserved_quantity) !== reserved) {
    throw new Error("Inventory receipt read-back failed")
  }

  return { status: 200, body: {
    inventory_item_id: input.inventory_item_id,
    location_id: input.location_id,
    before_stocked: stocked,
    after_stocked: target,
    available_quantity: target - reserved,
  } }
}

export async function receiveStock(container: MedusaContainer, input: ReceiptInput): Promise<ReceiptOutcome> {
  const fingerprint = createHash("sha256").update(JSON.stringify([
    input.inventory_item_id, input.location_id, input.quantity, input.expected_stocked, input.expected_reserved,
  ])).digest("hex")
  const database = container.resolve(ContainerRegistrationKeys.PG_CONNECTION)
  const locking = container.resolve(Modules.LOCKING)

  // Match the item key used by Medusa's reservation and delta-adjustment steps. The DB row
  // lock and journal cover concurrent receipts even with a process-local Locking provider.
  // This cannot protect against an absolute writer reusing a snapshot read before this lock.
  return await locking.execute(input.inventory_item_id, () => database.transaction(async (tx): Promise<ReceiptOutcome> => {
    const claim = await tx<JournalEntry>(stockReceiptJournal)
      .insert({ idempotency_key: input.idempotency_key, fingerprint })
      .onConflict("idempotency_key").ignore().returning("idempotency_key")
    if (!claim.length) {
      const recorded = await tx<JournalEntry>(stockReceiptJournal)
        .where("idempotency_key", input.idempotency_key).first()
      if (!recorded?.outcome) {
        throw new Error("Inventory receipt journal entry is incomplete")
      }
      return recorded.fingerprint === fingerprint
        ? recorded.outcome
        : { status: 409, body: { code: "idempotency_conflict" } }
    }

    const outcome = await applyReceipt(tx, input)
    const saved = await tx<JournalEntry>(stockReceiptJournal).where("idempotency_key", input.idempotency_key)
      .update({ outcome }).returning("idempotency_key")
    if (saved.length !== 1) {
      throw new Error("Inventory receipt journal update failed")
    }
    return outcome
  }, { isolationLevel: "read committed" }))
}
