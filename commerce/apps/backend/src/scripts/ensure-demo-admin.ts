import type { ExecArgs } from "@medusajs/framework/types"
import { Modules, MedusaError } from "@medusajs/framework/utils"
import { createUsersWorkflow } from "@medusajs/medusa/core-flows"

export default async function ensureDemoAdmin({ container }: ExecArgs) {
  const email = process.env.MEDUSA_ADMIN_EMAIL
  const password = process.env.MEDUSA_ADMIN_PASSWORD
  if (!email || !password) throw new MedusaError(MedusaError.Types.INVALID_DATA, "Admin credentials must be supplied by local runtime")
  const users = container.resolve(Modules.USER)
  const existing = await users.listUsers({ email })
  if (existing.length) return
  const { result } = await createUsersWorkflow(container).run({ input: { users: [{ email }] } })
  const auth = container.resolve(Modules.AUTH)
  const { authIdentity, error } = await auth.register("emailpass", { body: { email, password } })
  if (error || !authIdentity) throw new MedusaError(MedusaError.Types.INVALID_DATA, "Unable to register local demo administrator")
  await auth.updateAuthIdentities({ id: authIdentity.id, app_metadata: { user_id: result[0].id } })
}
