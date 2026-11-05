import type { ExecArgs } from "@medusajs/framework/types"
import { ContainerRegistrationKeys, MedusaError, ProductStatus } from "@medusajs/framework/utils"
import {
  createProductsWorkflow,
  createInventoryLevelsWorkflow,
  createOrderWorkflow,
  createReservationsWorkflow,
} from "@medusajs/medusa/core-flows"

/** Fictional development fixtures. Repeat runs preserve existing stock and orders. */
export default async function seedEcomops({ container }: ExecArgs) {
  const query = container.resolve(ContainerRegistrationKeys.QUERY)
  const logger = container.resolve(ContainerRegistrationKeys.LOGGER)
  const { data: channels } = await query.graph({ entity: "sales_channel", fields: ["id"] })
  const { data: locations } = await query.graph({ entity: "stock_location", fields: ["id"] })
  const { data: regions } = await query.graph({ entity: "region", fields: ["id", "currency_code"] })
  const { data: profiles } = await query.graph({ entity: "shipping_profile", fields: ["id"] })
  if (!channels[0] || !locations[0] || !regions[0] || !profiles[0]) {
    throw new MedusaError(MedusaError.Types.INVALID_DATA, "Run db:migrate and the official initial data migration first")
  }
  const fixtures = [
    { handle: "ecomops-demo-mug", title: "EcomOps Demo Mug", sku: "ECOMOPS-MUG", stock: 5 },
    { handle: "ecomops-demo-bag", title: "EcomOps Demo Bag", sku: "ECOMOPS-BAG", stock: 50 },
    { handle: "ecomops-demo-bottle", title: "EcomOps Demo Bottle", sku: "ECOMOPS-BOTTLE", stock: 0 },
  ]
  for (const fixture of fixtures) {
    const { data: existing } = await query.graph({
      entity: "product", fields: ["id"], filters: { handle: fixture.handle },
    })
    if (existing.length) continue
    await createProductsWorkflow(container).run({ input: { products: [{
      title: fixture.title, handle: fixture.handle,
      description: "Fictional EcomOps phase-one development data. Not a real product.",
      status: ProductStatus.PUBLISHED, shipping_profile_id: profiles[0].id,
      options: [{ title: "Size", values: ["Standard"] }],
      variants: [{ title: "Standard", sku: fixture.sku, manage_inventory: true,
        options: { Size: "Standard" },
        prices: [{ amount: 20, currency_code: regions[0].currency_code }],
      }],
      sales_channels: [{ id: channels[0].id }], metadata: { ecomops_demo: true },
    }] } })
  }
  const { data: inventory } = await query.graph({
    entity: "inventory_item", fields: ["id", "sku", "location_levels.*"],
    filters: { sku: fixtures.map((f) => f.sku) },
  })
  for (const item of inventory) {
    if (item.location_levels?.length) continue
    const fixture = fixtures.find((f) => f.sku === item.sku)!
    await createInventoryLevelsWorkflow(container).run({ input: { inventory_levels: [{
      inventory_item_id: item.id, location_id: locations[0].id, stocked_quantity: fixture.stock,
    }] } })
  }
  const { data: orders } = await query.graph({ entity: "order", fields: ["id", "metadata"] })
  const { data: variants } = await query.graph({
    entity: "product_variant", fields: ["id", "sku"], filters: { sku: "ECOMOPS-MUG" },
  })
  for (const index of [1, 2]) {
    const reference = `ecomops-demo-order-${index}`
    if (orders.some((o) => o.metadata?.ecomops_reference === reference)) continue
    const { result: order } = await createOrderWorkflow(container).run({ input: {
      email: `fictional-buyer-${index}@example.test`,
      region_id: regions[0].id, currency_code: regions[0].currency_code,
      sales_channel_id: channels[0].id, status: "pending",
      shipping_address: { first_name: "Demo", last_name: "Buyer", address_1: "1 Fictional Street",
        city: "Berlin", country_code: "de", postal_code: "10115" },
      items: [{ variant_id: variants[0].id, quantity: 1, title: "EcomOps Demo Mug", unit_price: 20 }],
      metadata: { ecomops_demo: true, ecomops_reference: reference },
    } })
    const item = inventory.find((i) => i.sku === "ECOMOPS-MUG")!
    await createReservationsWorkflow(container).run({ input: { reservations: [{
      inventory_item_id: item.id, location_id: locations[0].id,
      quantity: 1, line_item_id: order.items![0].id,
    }] } })
  }
  logger.info("EcomOps fictional fixtures ready: 3 products, 2 orders, real stock reservations")
}
