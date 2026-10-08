/* Geometry from the pinned MIT chrome-tabs template; see vendor/chrome-tabs/SOURCE.md. */
import { useId } from 'react';

export function ChromeTabBackground() {
  const id = useId();
  return <div className="chrome-tab-background"><svg version="1.1" xmlns="http://www.w3.org/2000/svg"><defs><symbol id={id + "-left"} viewBox="0 0 214 36"><path d="M17 0h197v36H0v-2c4.5 0 9-3.5 9-8V8c0-4.5 3.5-8 8-8z"/></symbol><symbol id={id + "-right"} viewBox="0 0 214 36"><use xlinkHref={"#" + id + "-left"}/></symbol></defs><svg width="52%" height="100%"><use xlinkHref={"#" + id + "-left"} width="214" height="36" className="chrome-tab-geometry"/></svg><g transform="scale(-1, 1)"><svg width="52%" height="100%" x="-100%" y="0"><use xlinkHref={"#" + id + "-right"} width="214" height="36" className="chrome-tab-geometry"/></svg></g></svg></div>;
}
