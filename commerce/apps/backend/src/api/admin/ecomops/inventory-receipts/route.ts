import type { AuthenticatedMedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { hasReceiptToken, receiptInScope, receiveStock, stockReceiptInput } from "../../../../lib/stock-receipts"

export const CORS = false

export async function POST(req: AuthenticatedMedusaRequest, res: MedusaResponse) {
  if (!req.auth_context?.actor_id || !hasReceiptToken(req.headers["x-ecomops-receipt-token"])) {
    return res.status(404).json({ code: "stock_cas_unavailable" })
  }

  const parsed = stockReceiptInput.safeParse(req.body)
  if (!parsed.success) {
    return res.status(422).json({ code: "invalid_receipt" })
  }
  if (!receiptInScope(parsed.data)) {
    return res.status(404).json({ code: "stock_cas_unavailable" })
  }

  const result = await receiveStock(req.scope, parsed.data)
  return res.status(result.status).json(result.body)
}
