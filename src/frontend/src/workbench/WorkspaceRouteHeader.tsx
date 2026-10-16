import {useEffect,useRef} from 'react';
import {selectedWorkspaceView} from './workspaceView';
interface HeaderView {id:string;path:string;title:string}
interface Props {
 active:boolean;hostNavigation:boolean;title:string;path:string;views:readonly HeaderView[];
 label:string;onNavigate:(path:string)=>void;
}
let titleOwner:{token:symbol;previous:string;assigned:string}|undefined;
/** Host-owned route chrome. Plugin navigation updates labels without stealing frame focus. */
export function WorkspaceRouteHeader({active,hostNavigation,title,path,views,label,onNavigate}:Props){
 const heading=useRef<HTMLHeadingElement>(null);
 const lastLocation=useRef<string|undefined>(undefined);
 const view=selectedWorkspaceView(views,path);
 const caption=view?view.title+' · '+title:title;
 const pathname=path.split(/[?#]/)[0];
 useEffect(()=>{
  if(!active)return;
  const token=Symbol('workspace-title');
  const previous=titleOwner?.previous??document.title;
  titleOwner={token,previous,assigned:caption};document.title=caption;
  return ()=>{
   if(titleOwner?.token!==token)return;
   if(document.title===titleOwner.assigned)document.title=titleOwner.previous;
   titleOwner=undefined;
  };
 },[active,caption]);
 useEffect(()=>{
  if(!active){lastLocation.current=undefined;return;}
  const changed=lastLocation.current!==pathname;
  lastLocation.current=pathname;
  if(!changed || !hostNavigation)return;
  const target=heading.current,current=document.activeElement;
  // A retained document can be typing while a route/catalog response arrives.
  // Only host controls or the document body yield focus to the route heading.
  if(!target || (current && current!==document.body && current!==target
   && !current.closest('button,a,[role="button"]')))return;
  target.focus({preventScroll:true});
 },[active,pathname,hostNavigation]);
 return <>
  <h1 ref={heading} tabIndex={-1}>{caption}</h1>
  {!!views.length&&<nav aria-label={label}>{views.map(value=><button type="button" key={value.id}
   aria-current={view?.id===value.id?'page':undefined}
   onClick={()=>onNavigate('/'+value.path.replace(/^\/+/,'') )}>{value.title}</button>)}</nav>}
 </>;
}
