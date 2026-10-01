import type { ConnectorAccount, ConnectorProvider } from './types'

/** Mirrors the backend's `PERSONAL_PROVIDERS` (`ecc/platform/
 * connector_security.py`). A personal provider's connector belongs to one
 * member's own mailbox and is managed only from the Personal area
 * (`GmailPanel`). `GET /engineering/connectors` still returns the caller's
 * own Gmail rows (and, with `ECC_PERSONAL_DATA_ISOLATION` off, other
 * members' too), so every engineering view filters them out here.
 * Unconditional: the frontend cannot see backend flags (Spec A S1.8(e),
 * delta F6). */
export const PERSONAL_PROVIDERS: ReadonlySet<ConnectorProvider> = new Set<ConnectorProvider>(['gmail'])

/** Drops every connector whose provider is in `PERSONAL_PROVIDERS`, keeping
 * the rest in their original order. Applied unconditionally by every
 * engineering view (`ConnectorHealthPanel`, `CoveragePanel`,
 * `EngineeringOverview`) -- never gated on `ECC_PERSONAL_DATA_ISOLATION`,
 * which the frontend cannot see (Spec A delta F6). The provider list it
 * checks mirrors `backend/ecc/platform/connector_security.PERSONAL_PROVIDERS`;
 * keep the two in sync. */
export function withoutPersonalProviders(connectors: ConnectorAccount[]): ConnectorAccount[] {
  return connectors.filter((connector) => !PERSONAL_PROVIDERS.has(connector.provider))
}
