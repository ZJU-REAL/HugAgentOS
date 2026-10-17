// Bind a login callback to the tab that started it; this value is not a session credential.
for (const link of document.querySelectorAll<HTMLAnchorElement>('a[data-login], a#login')) {
 link.addEventListener('click', event => {
  event.preventDefault();
  const state = crypto.randomUUID();
  sessionStorage.setItem('hugagent-login', JSON.stringify({ state, started: Date.now() }));
  const redirect = '/session.html?state=' + encodeURIComponent(state);
  location.assign('/login?redirect=' + encodeURIComponent(redirect));
 });
}
export {};
