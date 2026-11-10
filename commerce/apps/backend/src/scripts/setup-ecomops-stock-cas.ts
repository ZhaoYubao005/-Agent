import type { MedusaContainer } from "@medusajs/framework"
import { ContainerRegistrationKeys } from "@medusajs/framework/utils"
import { stockReceiptJournal } from "../lib/stock-receipts"

export default async function setupEcomopsStockCas({ container }: { container: MedusaContainer }) {
  const database = container.resolve(ContainerRegistrationKeys.PG_CONNECTION)
  if (await database.schema.hasTable(stockReceiptJournal)) {
    return
  }
  try {
    await database.schema.createTable(stockReceiptJournal, (table) => {
      table.text("idempotency_key").primary()
      table.string("fingerprint", 64).notNullable()
      table.jsonb("outcome").nullable()
    })
  } catch (error) {
    // A concurrent setup may already have created this table or its catalog entry.
    const code = (error as { code?: string }).code
    if ((code !== "42P07" && code !== "23505") || !await database.schema.hasTable(stockReceiptJournal)) {
      throw error
    }
  }
}
