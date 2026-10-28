// Fit a slide opened directly. The bundled index.html has its own player.
(() => {
  const slide = document.querySelector('body > .slide');
  if (!slide) return;
  slide.classList.add('is-active');
  function fit() {
    const scale = Math.min(innerWidth / 1600, innerHeight / 900);
    Object.assign(slide.style, {position:'absolute',left:'50%',top:'50%',margin:'0',transform:`translate(-50%,-50%) scale(${scale})`});
    document.body.style.overflow = 'hidden';
  }
  addEventListener('resize', fit);
  fit();
  document.fonts.ready.then(() => { fit(); window.Whiteboard?.activate(slide, Infinity, true); });
})();
