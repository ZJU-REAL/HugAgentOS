export interface PublicChatCredential { token: string; visitor: string }
let credential: PublicChatCredential | null = null;
export function setPublicChatCredential(value: PublicChatCredential | null) { credential = value; }
export function publicChatHeaders(): Record<string, string> {
  return credential ? { 'X-Public-Chat': credential.token, 'X-Public-Visitor': credential.visitor } : {};
}
export function isPublicChat() { return credential !== null; }
