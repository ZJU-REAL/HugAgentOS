export type I18nText = string | Record<string, string>;
export type ApplicationTarget =
  | { readonly kind: 'builtin'; readonly componentId: string }
  | { readonly kind: 'plugin'; readonly moduleId: string }
  | { readonly kind: 'external'; readonly url: string; readonly mode: 'browser' | 'embedded' }
  | { readonly kind: 'command'; readonly commandId: string; readonly params?: Record<string, string | number | boolean | null> };
export interface ApplicationDeclaration {
  readonly schemaVersion: '1.0.0';
  readonly id: string;
  readonly title: I18nText;
  readonly description?: I18nText;
  readonly icon?: { light: string; dark?: string };
  readonly target: ApplicationTarget;
  readonly navigation?: { defaultPinned?: boolean; defaultOrder?: number; group?: string; showInAppCenter?: boolean };
  readonly views?: readonly { id: string; title: I18nText; path: string; moduleId?: string }[];
  readonly permissions?: readonly string[];
  readonly availability?: { platforms?: ('web' | 'windows' | 'macos' | 'uos')[]; executionScopes?: ('local' | 'cloud')[]; requiresOnline?: boolean };
  readonly lifecycle?: { keepAlive?: boolean; stateSchemaVersion?: string };
  readonly compatibility?: { protocolVersion?: string; requiredCapabilities?: readonly string[]; optionalCapabilities?: readonly string[] };
}
/** Trusted in-process registration is a projection of the wire declaration. */
export type RegisteredApplication = Pick<ApplicationDeclaration, 'id'> & {
  readonly target: Extract<ApplicationTarget, {kind: 'builtin' | 'command'}>;
};
export interface ValidationIssue { path: string; code: string; message: string }
export interface ValidationContext {
  componentIds?: readonly string[];
  commandIds?: readonly string[];
  modules?: readonly {id: string; surface: string}[];
  capabilities?: readonly string[];
  source?: 'host' | 'plugin';
}
