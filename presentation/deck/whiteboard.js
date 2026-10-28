/* Rough.js outlines follow the supplied Whiteboard Design System.
   Animate only newly revealed content. Final styles stay visible after cancellation. */
(() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const ink = name => getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim();
  const seed = element => [...element.textContent].reduce((n,char)=>(n*31+char.charCodeAt(0))%99991,17);
  function deactivate(element) {
    element.getAnimations({subtree:true}).forEach(animation=>animation.cancel());
  }
  function colorOf(element) {
    for(const color of ['red','violet','green','blue'])if(element.classList.contains(color))return ink(color);
    if(element.classList.contains('neutral'))return ink('ink-2');
    if(element.classList.contains('sticky'))return '#c69717';
    if(element.classList.contains('marker'))return element.closest('.cover') ? ink('ink') : ink('marker');
    return getComputedStyle(element).getPropertyValue('--sketch-color').trim()==='violet'?ink('violet'):ink('blue');
  }
  function draw(element) {
    if(!window.rough||!element.offsetWidth||!element.offsetHeight)return;
    const w=element.offsetWidth,h=element.offsetHeight;
    if(element._sketchWidth===w&&element._sketchHeight===h&&element._sketchSvg?.isConnected)return;
    element._sketchSvg?.remove();
    element.querySelector(':scope>.sketch-layer')?.remove();
    const svg=document.createElementNS(NS,'svg');
    svg.classList.add('sketch-layer');svg.setAttribute('aria-hidden','true');
    svg.setAttribute('width',w+16);svg.setAttribute('height',h+16);
    Object.assign(svg.style,{left:'-8px',top:'-8px'});
    const rc=rough.svg(svg),options={seed:seed(element),stroke:colorOf(element),strokeWidth:1.8,roughness:.8,bowing:.7};
    if(element.classList.contains('marker'))svg.append(rc.line(7,h+5,w+10,h+2,{...options,strokeWidth:5,roughness:.55}));
    else {
      const r=element.classList.contains('bubble')?15:7,x=9,y=9,right=w+6,bottom=h+6;
      svg.append(rc.path(`M${x+r},${y} L${right-r},${y} Q${right},${y} ${right},${y+r} L${right},${bottom-r} Q${right},${bottom} ${right-r},${bottom} L${x+r},${bottom} Q${x},${bottom} ${x},${bottom-r} L${x},${y+r} Q${x},${y} ${x+r},${y} Z`,options));
    }
    element.append(svg);element.classList.add('sketched');
    element._sketchWidth=w;element._sketchHeight=h;element._sketchSvg=svg;
  }
  // offset coordinates ignore entrance transforms and stage scaling.
  function bounds(element,parent) {
    let left=0,top=0,node=element;
    while(node&&node!==parent){left+=node.offsetLeft;top+=node.offsetTop;node=node.offsetParent;}
    return {left,top,right:left+element.offsetWidth,bottom:top+element.offsetHeight,width:element.offsetWidth,height:element.offsetHeight};
  }
  function arrow(svg,a,b,step,options) {
    if(![a.x,a.y,b.x,b.y].every(Number.isFinite))return;
    const group=document.createElementNS(NS,'g');group.dataset.targetStep=step;
    const rc=rough.svg(svg),angle=Math.atan2(b.y-a.y,b.x-a.x),head=10;
    group.append(rc.line(a.x,a.y,b.x,b.y,options),rc.line(b.x,b.y,b.x-head*Math.cos(angle-.5),b.y-head*Math.sin(angle-.5),options),rc.line(b.x,b.y,b.x-head*Math.cos(angle+.5),b.y-head*Math.sin(angle+.5),options));
    svg.append(group);
  }
  function connectors(parent) {
    if(!parent.offsetWidth||!parent.getBoundingClientRect().width)return;
    const children=[...parent.children].filter(e=>!e.classList.contains('flow-connectors'));
    const targets=parent.classList.contains('agent-tree')?[parent.querySelector('.supervisor'),...parent.querySelectorAll('.specialists>div')]:children;
    if(targets.length<2||targets.some(e=>!e))return;
    const boxes=targets.map(e=>bounds(e,parent));
    const signature=JSON.stringify(boxes);
    if(parent._connectorSignature===signature&&parent._connectorSvg?.isConnected)return;
    parent.querySelector(':scope>.flow-connectors')?.remove();
    const svg=document.createElementNS(NS,'svg');svg.classList.add('flow-connectors');svg.setAttribute('aria-hidden','true');
    const options={seed:seed(parent),stroke:ink('ink-2'),strokeWidth:2,roughness:.85};
    if(parent.classList.contains('agent-tree'))boxes.slice(1).forEach((box,index)=>{
      const x=box.left+box.width/2;
      arrow(svg,{x,y:boxes[0].bottom+7},{x,y:box.top-7},Number(targets[index+1].dataset.step)||2,options);
    });
    else boxes.slice(0,-1).forEach((a,index)=>{
      const b=boxes[index+1],y=Math.max(a.top,b.top)+Math.min(a.height,b.height)*.48;
      arrow(svg,{x:a.right+8,y},{x:b.left-8,y},Number(targets[index+1].dataset.step)||1,{...options,seed:options.seed+index});
    });
    parent.append(svg);parent._connectorSignature=signature;parent._connectorSvg=svg;
  }
  function drawOn(svg,delay=0) {
    if(!svg||motion.matches)return;
    svg.querySelectorAll('path').forEach(path=>{
      if(path.getAttribute('stroke')==='none')return;
      const length=path.getTotalLength();if(!Number.isFinite(length)||!length)return;
      path.getAnimations().forEach(animation=>animation.cancel());
      path.style.animation='';path.style.strokeDasharray=String(length);path.style.strokeDashoffset='0';
      path.animate([{strokeDashoffset:String(length)},{strokeDashoffset:'0'}],{duration:600,delay,fill:'backwards',easing:'cubic-bezier(.2,.8,.2,1)'});
    });
  }
  function animateElement(element,delay=0) {
    const kind=element.dataset.anim||'pop';
    const base=getComputedStyle(element).transform;
    const finalTransform=base==='none'?'':base;
    let frames;
    if(kind==='fade'||kind==='draw')frames=[{opacity:0},{opacity:1}];
    else if(kind==='slide')frames=[{opacity:0,transform:`translateY(12px) ${finalTransform}`},{opacity:1,transform:finalTransform||'none'}];
    else if(kind==='stamp')frames=[{opacity:0,transform:`scale(1.12) ${finalTransform}`},{opacity:1,transform:finalTransform||'none'}];
    else frames=[{opacity:0,transform:`translateY(6px) scale(.96) ${finalTransform}`},{opacity:1,transform:finalTransform||'none'}];
    element.animate(frames,{duration:kind==='stamp'?300:340,delay,fill:'backwards',easing:'cubic-bezier(.2,.8,.2,1)'});
    element.querySelectorAll('.sketch-layer').forEach(svg=>drawOn(svg,delay));
    element.querySelectorAll('.wave i').forEach((bar,index)=>bar.animate([{transform:'scaleY(.5)'},{transform:'scaleY(1)'},{transform:'scaleY(.5)'}],{duration:650,iterations:3,delay:index*35,easing:'ease-in-out'}));
  }
  function prepare(slide) {
    if(!slide.offsetWidth||!slide.getBoundingClientRect().width)return;
    slide.querySelectorAll('.sketch-box,.bubble,.sticky,.marker').forEach(draw);
    slide.querySelectorAll('.flow,.mcp-diagram,.agent-tree').forEach(connectors);
  }
  // entering: the slide just opened, so the first visible steps come in one after another after the header.
  function reveal(slide,step,animate=true,entering=false) {
    const animateNow=animate&&!motion.matches;
    if(!animateNow)deactivate(slide);
    let order=0;
    slide.querySelectorAll('[data-step]').forEach(element=>{
      const on=Number(element.dataset.step)<=step,was=element.classList.contains('is-revealed');
      if(!on)deactivate(element);
      element.classList.toggle('is-revealed',on);element.setAttribute('aria-hidden',String(!on));
      if(on&&!was&&animateNow)animateElement(element,entering?Math.min(320+order++*120,1100):0);
    });
    slide.querySelectorAll('.flow-connectors>[data-target-step]').forEach(group=>{
      const on=Number(group.dataset.targetStep)<=step,was=group.style.visibility==='visible';
      group.style.visibility=on?'visible':'hidden';
      if(!on)deactivate(group);
      else if(!was&&animateNow)drawOn(group,entering?500:0);
    });
  }
  function activate(slide,step,animate=true) {
    prepare(slide);
    const animateNow=animate&&!motion.matches;
    if(animateNow){
      // Open the slide in order: header, the content that has no step, then the first steps.
      const easing='cubic-bezier(.2,.8,.2,1)';
      slide.querySelector('.slide-header')?.animate([{opacity:0,transform:'translateY(14px)'},{opacity:1,transform:'none'}],{duration:420,easing});
      slide.querySelectorAll('.slide-header .sketch-layer').forEach(svg=>drawOn(svg,250));
      const content=slide.querySelector('.slide-content');
      content?.animate([{opacity:0},{opacity:1}],{duration:380,delay:180,fill:'backwards',easing});
      content?.querySelectorAll('.sketch-layer').forEach(svg=>{if(!svg.closest('[data-step]'))drawOn(svg,220);});
      slide.querySelector('.slide-footer')?.animate([{opacity:0},{opacity:1}],{duration:400,delay:300,fill:'backwards'});
    }
    reveal(slide,step,animate,animateNow);
  }
  motion.addEventListener('change',()=>{if(motion.matches)document.querySelectorAll('.slide').forEach(deactivate);});
  window.Whiteboard={prepare,reveal,activate,deactivate};
})();
