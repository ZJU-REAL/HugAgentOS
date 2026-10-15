import { useEffect } from 'react';

/** A result is read only after it rendered in a visible, focused, bottom viewport. */
export function useReadAtBottom(element:HTMLElement|null, enabled:boolean, acknowledge:()=>void) {
  useEffect(()=>{
    if (!element || !enabled) return;
    let frame=0;
    const check=()=>{
      cancelAnimationFrame(frame);
      frame=requestAnimationFrame(()=>{
        if(document.visibilityState==='visible' && document.hasFocus() && !element.closest('[aria-hidden="true"], [inert]') && element.getClientRects().length &&
            element.scrollHeight-element.scrollTop-element.clientHeight<80) acknowledge();
      });
    };
    check(); element.addEventListener('scroll',check);window.addEventListener('focus',check);
    document.addEventListener('visibilitychange',check);
    const observer=new ResizeObserver(check);observer.observe(element);
    return ()=>{cancelAnimationFrame(frame);observer.disconnect();element.removeEventListener('scroll',check);
      window.removeEventListener('focus',check);document.removeEventListener('visibilitychange',check);};
  },[element,enabled,acknowledge]);
}
