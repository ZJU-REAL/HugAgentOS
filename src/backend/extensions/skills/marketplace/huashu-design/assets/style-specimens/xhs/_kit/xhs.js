// 小红书图文页面的小工具。全部是纯函数式摆放，不依赖框架。
window.XHS = {
  S: id => `_kit/stickers/${id}.webp`,
  // 给每个 .xhs-page 写页码（data-pageno="off" 跳过）。total 可省略。
  number(total){
    const ps=[...document.querySelectorAll('.xhs-page')];
    ps.forEach((p,k)=>{ if(p.dataset.pageno==='off') return;
      p.insertAdjacentHTML('beforeend',`<div class="pageno">${String(k+1).padStart(2,'0')} / ${total||ps.length}</div>`); });
  },
  // 在连续长卷里放一个跨缝的元素：seam 是第几道缝（1 = 图1和图2之间），keep 是留在左图的比例（0.66 = 左边露 2/3）
  straddle(el, seam, keep, W=1242){ const w=el.offsetWidth; /* offsetWidth 不受样张缩放影响 */ el.style.left=(seam*W - keep*w)+'px'; },
  // 电线：y(x) 是一条连续的下垂曲线，周期跨两张图，避免最低点总落在缝上
  wire(strip, {base=1080, sag=90, period=2484, phase=300, color='#211F1C', width=5}={}){
    const W=strip.offsetWidth, y=x=>base+sag*(1-Math.cos(2*Math.PI*(x+phase)/period))/2;
    let d=''; for(let x=-10;x<=W+10;x+=10) d+=(x<0?'M':'L')+x+' '+y(x).toFixed(1);
    strip.insertAdjacentHTML('afterbegin',`<svg class="abs" style="left:0;top:0" width="${W}" height="1660"><path d="${d}" fill="none" stroke="${color}" stroke-width="${width}" stroke-linecap="round"/></svg>`);
    return y;
  },
  // 让一张贴纸「站」在电线上：foot=[fx,fy] 是爪子在图里的比例位置（先在贴纸上量好），cx 是爪子落点的横坐标
  perch(strip, y, src, cx, h, foot, natural){
    const [w0,h0]=natural, w=w0*h/h0, im=new Image(); im.src=src; im.className='sticker';
    Object.assign(im.style,{left:(cx-foot[0]*w)+'px', top:(y(cx)-foot[1]*h+6)+'px', height:h+'px'}); strip.appendChild(im); return im;
  }
};
