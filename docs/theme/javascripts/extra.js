/* ============================================================
   Free-Router — Cyberpunk Theme JS
   故障文字 / 扫描线 / 粒子网格 / 代码复制 / TOC 高亮 / 主题切换
   ============================================================ */
(function () {
  'use strict';

  var prefersReducedMotion =
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- 主题切换 ---------- */
  function initThemeToggle() {
    var root = document.documentElement;
    var saved = localStorage.getItem('free-router-theme');
    if (saved === 'minimal') root.setAttribute('data-theme', 'minimal');
    else root.setAttribute('data-theme', 'cyberpunk');

    var btn = document.createElement('button');
    btn.className = 'cyber-theme-toggle';
    btn.setAttribute('aria-label', '切换主题');
    btn.textContent = saved === 'minimal' ? 'CY' : 'MIN';
    document.body.appendChild(btn);

    btn.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'cyberpunk' ? 'minimal' : 'cyberpunk';
      root.setAttribute('data-theme', next);
      btn.textContent = next === 'cyberpunk' ? 'MIN' : 'CY';
      localStorage.setItem('free-router-theme', next);
    });
  }

  /* ---------- 故障文字 ---------- */
  function initGlitchText() {
    var el = document.querySelector('.md-header__title .md-ellipsis');
    if (!el) return;
    el.classList.add('glitch');
    var original = el.textContent || '';
    el.setAttribute('data-text', original);
    if (prefersReducedMotion) return;

    var chars = '!<>-_\\/[]{}—=+*^?#________';
    var interval = 2800;
    function glitch() {
      if (document.hidden) return;
      var frames = 0;
      var max = 6;
      var t = setInterval(function () {
        var out = '';
        for (var i = 0; i < original.length; i++) {
          if (original[i] === ' ') { out += ' '; continue; }
          out += Math.random() < 0.25 ? chars[Math.floor(Math.random() * chars.length)] : original[i];
        }
        el.textContent = out;
        if (++frames > max) { clearInterval(t); el.textContent = original; }
      }, 50);
    }
    setInterval(glitch, interval);
  }

  /* ---------- 粒子网格背景 ---------- */
  function initParticleGrid() {
    var canvas = document.getElementById('cyber-grid');
    if (!canvas || prefersReducedMotion) return;
    var ctx = canvas.getContext('2d');
    var particles = [];
    var W, H;

    function resize() {
      W = canvas.width = window.innerWidth;
      H = canvas.height = window.innerHeight;
      var count = Math.min(90, Math.floor((W * H) / 18000));
      particles = [];
      for (var i = 0; i < count; i++) {
        particles.push({
          x: Math.random() * W, y: Math.random() * H,
          vx: (Math.random() - 0.5) * 0.4, vy: (Math.random() - 0.5) * 0.4,
          r: Math.random() * 1.4 + 0.3,
          c: Math.random() < 0.7 ? '#00ffcc' : '#ff006e'
        });
      }
    }

    function step() {
      ctx.clearRect(0, 0, W, H);
      // 网格线
      ctx.strokeStyle = 'rgba(0,255,204,0.05)';
      ctx.lineWidth = 1;
      var g = 48;
      for (var x = 0; x < W; x += g) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
      for (var y = 0; y < H; y += g) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
      // 粒子
      for (var i = 0; i < particles.length; i++) {
        var p = particles[i];
        p.x += p.vx; p.y += p.vy;
        if (p.x < 0 || p.x > W) p.vx *= -1;
        if (p.y < 0 || p.y > H) p.vy *= -1;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = p.c;
        ctx.shadowBlur = 8; ctx.shadowColor = p.c;
        ctx.fill(); ctx.shadowBlur = 0;
      }
      requestAnimationFrame(step);
    }

    window.addEventListener('resize', resize);
    resize();
    step();
  }

  /* ---------- 代码块复制（Toast） ---------- */
  function initCodeCopyToast() {
    document.addEventListener('click', function (e) {
      var btn = e.target.closest('.md-clipboard');
      if (!btn) return;
      var toast = document.createElement('div');
      toast.className = 'cyber-toast';
      toast.textContent = '[ OK ] 已复制到剪贴板';
      toast.style.cssText = [
        'position:fixed', 'bottom:1.5rem', 'left:50%', 'transform:translateX(-50%)',
        'z-index:1000', 'padding:.6rem 1rem', 'font-family:var(--font-mono)',
        'background:#0a0a0fdd', 'color:#00ffcc', 'border:1px solid #00ffcc66',
        'border-radius:6px', 'box-shadow:0 0 16px #00ffcc44', 'opacity:0',
        'transition:opacity .2s'
      ].join(';');
      document.body.appendChild(toast);
      requestAnimationFrame(function () { toast.style.opacity = '1'; });
      setTimeout(function () {
        toast.style.opacity = '0';
        setTimeout(function () { toast.remove(); }, 250);
      }, 1600);
    });
  }

  /* ---------- TOC 高亮脉冲 ---------- */
  function initTOCHighlight() {
    var links = document.querySelectorAll('.md-nav--secondary .md-nav__link');
    if (!links.length) return;
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) {
          links.forEach(function (l) { l.classList.remove('cyber-pulse'); });
          var link = document.querySelector(
            '.md-nav--secondary .md-nav__link[href="#' + en.target.id + '"]');
          if (link) link.classList.add('cyber-pulse');
        }
      });
    }, { rootMargin: '-40% 0px -55% 0px' });
    document.querySelectorAll('h2[id], h3[id]').forEach(function (h) { observer.observe(h); });
  }

  /* ---------- 启动 ---------- */
  function ready(fn) {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }
  ready(function () {
    initThemeToggle();
    initGlitchText();
    initParticleGrid();
    initCodeCopyToast();
    initTOCHighlight();
  });
})();
