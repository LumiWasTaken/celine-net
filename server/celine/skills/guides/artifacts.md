---
name: artifacts
description: Pages {{username}} opens beside the chat: apps, games, charts, tables,
  dashboards, diagrams, documents. Use it whenever they want something made to look at or
  use. Never paste HTML or SVG into your reply.
---
An artifact is one page you hand {{username}}: something to look at, use or play. It
opens beside the chat or as a page of its own, and every change you make is a new
version, so nothing is ever lost.

## When

- {{username}} asks for something to see, use or play: a chart, a table, a dashboard,
  a calculator, a to-do list, a quiz, a game, a diagram, a little tool.
- Your answer would be a long table or numbers that read better as a chart, more than
  about 15 lines, and they will want to keep it or click around in it.

## When not

- A short answer or a small table: write it in the chat.
- Code {{username}} runs somewhere else: a code block in the chat. Work in your
  shellbox: your shell.

## How

1. `artifacts__write(title="Budget 2026", content="<h1>Budget</h1>...")`. You get its id,
   like 7f3a9c.
2. In your reply, put `[[artifact:7f3a9c]]` on a line of its own: {{username}} sees a
   card that opens it. Never paste the page's text into the chat.
3. To change it, write again with `id="7f3a9c"` and the whole new page. A new artifact
   with the title of one in this conversation is refused: that is your sign to use its id.
4. The result may end with `hint:` lines. Each names a line of your page that will not
   work here. Fix it with a new version unless you meant it.
5. Something to read rather than run (a plan, a guide, notes with tables): give
   `kind="markdown"` and write markdown. It shows like your replies, without script.
6. What you found on the web goes in as facts in your own words: no citation marks
   (`[[web:3]]`, [1], 【1】), no "according to", no list of sources or links. A link
   goes in only when the link itself is part of what {{username}} asked for.

## The page

- One file: HTML with its `<style>` and `<script>` inline. A whole document or only the
  parts; the app adds the doctype, the charset, the viewport and a dark base style
  (the app's type and colours) that any style of yours overrides.
- Short lines, one statement or rule per line, two spaces to indent. Keep it under about
  20 KB: put data in a small array, not a thousand rows.
- It runs on its own, sandboxed: no network, no cookies, no `localStorage`, no
  `alert`, `confirm` or `prompt`, no popups, no new tabs, no downloads. `fetch` reaches
  nothing: put the data into the page. Build dialogs, menus and messages in the page.
- `localStorage`, `sessionStorage`, `indexedDB` and `document.cookie` throw a
  `SecurityError` when touched, and an uncaught one stops the script; state lives in
  variables and is gone on reload. `requestFullscreen`, `requestPointerLock`, the
  clipboard, the camera and the microphone are refused.
- Libraries load only from https://cdnjs.cloudflare.com/, https://cdn.jsdelivr.net/npm/,
  https://unpkg.com/, https://cdn.tailwindcss.com/ and https://code.jquery.com/; fonts
  from Google Fonts. Pin the version in the address, as below. Images: inline SVG, a
  canvas, or a `data:` URI. A picture from any other site does not load.
- Forms work: handle `submit` with `event.preventDefault()`.
- Keys reach the page once {{username}} has clicked into it. A game says "Click to
  start" and listens to clicks and touches as well as keys.
- The page shows in a panel beside the chat (about half a screen wide), in a whole tab,
  or on a phone. Let it flow: `max-width`, flexible grids, `width: 100%` for charts.

## Looks: dark and light

The app is dark, so pages show dark; write both. Start every page with this:

```html
<style>
  :root { color-scheme: light dark; --bg: #fdfaf7; --fg: #2a2326; --muted: #7a6d66; --card: #f1ebe6; --line: #e0d6cf; --accent: #c2477a }
  @media (prefers-color-scheme: dark) { :root { --bg: #1b1618; --fg: #f4ece6; --muted: #a39389; --card: #2c2427; --line: #3a3034; --accent: #f29db9 } }
  * { box-sizing: border-box }
  body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif }
  button, input, select { font: inherit; color: inherit; background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px }
  button { cursor: pointer }
  .card { background: var(--card); border-radius: 12px; padding: 12px 16px }
</style>
```

Below, "the style above" stands for this block. Charts take their colours from
`getComputedStyle(document.documentElement)`, as the Chart.js example shows.

## Charts

Chart.js, for most charts (bar, line, pie, doughnut, radar, scatter):

```html
<!-- the style above -->
<div class="card" style="max-width: 720px"><canvas id="chart"></canvas></div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"></script>
<script>
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  Chart.defaults.color = css('--muted');
  Chart.defaults.borderColor = css('--line');
  new Chart(document.getElementById('chart'), {
    type: 'bar',
    data: {
      labels: ['Jan', 'Feb', 'Mar', 'Apr'],
      datasets: [{ label: 'Spent', data: [420, 380, 510, 460], backgroundColor: css('--accent') }]
    },
    options: { responsive: true, plugins: { legend: { position: 'bottom' } } }
  });
</script>
```

D3, when you draw the shapes yourself (SVG):

```html
<!-- the style above -->
<svg id="chart" viewBox="0 0 400 200" style="width: 100%; max-width: 640px"></svg>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<script>
  const data = [{ k: 'A', v: 30 }, { k: 'B', v: 80 }, { k: 'C', v: 45 }];
  const x = d3.scaleBand(data.map(d => d.k), [30, 390]).padding(0.2);
  const y = d3.scaleLinear([0, d3.max(data, d => d.v)], [180, 10]);
  const svg = d3.select('#chart');
  svg.selectAll('rect').data(data).join('rect')
    .attr('x', d => x(d.k)).attr('y', d => y(d.v))
    .attr('width', x.bandwidth()).attr('height', d => 180 - y(d.v))
    .style('fill', 'var(--accent)');
  svg.append('g').attr('transform', 'translate(0,180)').call(d3.axisBottom(x));
  svg.append('g').attr('transform', 'translate(30,0)').call(d3.axisLeft(y).ticks(4));
</script>
```

Plotly, for zooming and hovering over many points:

```html
<!-- the style above -->
<div id="plot" style="width: 100%; height: 360px"></div>
<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@4.1.1/plotly.min.js"></script>
<script>
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  Plotly.newPlot('plot', [
    { x: [1, 2, 3, 4], y: [10, 15, 13, 17], type: 'scatter', name: 'one' },
    { x: [1, 2, 3, 4], y: [16, 5, 11, 9], type: 'scatter', name: 'two' }
  ], {
    template: dark ? { layout: { paper_bgcolor: '#1b1618', plot_bgcolor: '#1b1618', font: { color: '#f4ece6' } } } : {},
    margin: { t: 24, r: 16, b: 40, l: 40 }
  }, { responsive: true, displaylogo: false });
</script>
```

ECharts, for dashboards with many chart kinds (gauge, heatmap, sankey, treemap):

```html
<!-- the style above -->
<div id="chart" style="width: 100%; height: 360px"></div>
<script src="https://cdn.jsdelivr.net/npm/echarts@6.1.0/dist/echarts.min.js"></script>
<script>
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  const chart = echarts.init(document.getElementById('chart'), dark ? 'dark' : null);
  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {},
    series: [{ type: 'pie', radius: ['40%', '70%'], data: [{ name: 'Rent', value: 900 }, { name: 'Food', value: 400 }, { name: 'Fun', value: 150 }] }]
  });
  addEventListener('resize', () => chart.resize());
</script>
```

## Diagrams

Mermaid, for flowcharts, sequences, timelines, mind maps, Gantt charts:

```html
<!-- the style above -->
<pre class="mermaid">
flowchart LR
  A[Idea] --> B{Worth it?}
  B -- yes --> C[Build it]
  B -- no --> D[Drop it]
</pre>
<script src="https://cdn.jsdelivr.net/npm/mermaid@12.1.0/dist/mermaid.min.js"></script>
<script>
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  mermaid.initialize({ startOnLoad: true, theme: dark ? 'dark' : 'default' });
</script>
```

## Tables

A table that sorts when a heading is clicked, plain JavaScript:

```html
<!-- the style above -->
<style>
  table { border-collapse: collapse; width: 100%; max-width: 720px }
  th, td { padding: 6px 10px; border-bottom: 1px solid var(--line); text-align: left }
  th { cursor: pointer; user-select: none; color: var(--muted) }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums }
</style>
<table id="t">
  <thead><tr><th>Name</th><th class="num">Age</th><th>City</th></tr></thead>
  <tbody></tbody>
</table>
<script>
  const rows = [['Ana', 31, 'Lisbon'], ['Ben', 24, 'Berlin'], ['Cleo', 45, 'Paris']];
  let by = 0, up = true;
  function draw() {
    const sorted = [...rows].sort((a, b) => (a[by] > b[by] ? 1 : a[by] < b[by] ? -1 : 0) * (up ? 1 : -1));
    document.querySelector('#t tbody').innerHTML = sorted.map(r =>
      `<tr><td>${r[0]}</td><td class="num">${r[1]}</td><td>${r[2]}</td></tr>`).join('');
    document.querySelectorAll('#t th').forEach((th, i) => th.dataset.arrow = i === by ? (up ? ' ▲' : ' ▼') : '');
  }
  document.querySelectorAll('#t th').forEach((th, i) => th.addEventListener('click', () => {
    up = by === i ? !up : true;
    by = i;
    draw();
  }));
  draw();
</script>
```

## Dashboards

Cards in a grid that wraps on a phone, a chart under them:

```html
<!-- the style above -->
<style>
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 16px }
  .big { font-size: 28px; font-weight: 700 }
  .label { color: var(--muted); font-size: 13px }
</style>
<h2>This month</h2>
<div class="grid">
  <div class="card"><div class="label">Income</div><div class="big">3,200</div></div>
  <div class="card"><div class="label">Spent</div><div class="big">2,140</div></div>
  <div class="card"><div class="label">Saved</div><div class="big">1,060</div></div>
</div>
<div class="card"><canvas id="trend"></canvas></div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"></script>
<script>
  new Chart(document.getElementById('trend'), {
    type: 'line',
    data: { labels: ['W1', 'W2', 'W3', 'W4'], datasets: [{ label: 'Spent', data: [480, 620, 510, 530], tension: 0.3 }] }
  });
</script>
```

## Games

A canvas, a loop with `requestAnimationFrame`, input from keys, clicks and touches,
a score, and a restart. This one is a flappy bird:

```html
<!-- the style above -->
<style>
  canvas { display: block; margin: 0 auto; max-width: 100%; border-radius: 12px; background: #1d3557; touch-action: none }
</style>
<canvas id="game" width="360" height="480" tabindex="0"></canvas>
<script>
  const cv = document.getElementById('game'), g = cv.getContext('2d');
  let bird, pipes, score, best = 0, state = 'ready', last = 0;
  function reset() { bird = { y: 240, v: 0 }; pipes = []; score = 0; }
  function flap() {
    if (state === 'ready' || state === 'over') { reset(); state = 'play'; }
    bird.v = -7;
  }
  addEventListener('keydown', e => { if (e.code === 'Space' || e.code === 'ArrowUp') { e.preventDefault(); flap(); } });
  cv.addEventListener('pointerdown', e => { e.preventDefault(); cv.focus(); flap(); });
  function step(dt) {
    bird.v += 0.4 * dt; bird.y += bird.v * dt;
    if (!pipes.length || pipes[pipes.length - 1].x < 180) pipes.push({ x: 380, gap: 120 + Math.random() * 240 });
    for (const p of pipes) {
      p.x -= 2.5 * dt;
      if (!p.passed && p.x < 60) { p.passed = true; score++; }
      if (p.x < 80 && p.x > 20 && Math.abs(bird.y - p.gap) > 70) state = 'over';
    }
    pipes = pipes.filter(p => p.x > -60);
    if (bird.y > 480 || bird.y < 0) state = 'over';
    if (state === 'over') best = Math.max(best, score);
  }
  function draw() {
    g.clearRect(0, 0, 360, 480);
    g.fillStyle = '#7bd389';
    for (const p of pipes) { g.fillRect(p.x, 0, 50, p.gap - 70); g.fillRect(p.x, p.gap + 70, 50, 480); }
    g.fillStyle = '#ffd166';
    g.beginPath(); g.arc(60, bird.y, 12, 0, Math.PI * 2); g.fill();
    g.fillStyle = '#fff'; g.font = 'bold 20px system-ui'; g.textAlign = 'center';
    g.fillText(state === 'play' ? score : state === 'ready' ? 'Click or press Space' : `Score ${score}, best ${best}. Again?`, 180, 40);
  }
  function loop(t) {
    const dt = Math.min(2, (t - last) / 16.7 || 1); last = t;
    if (state === 'play') step(dt);
    draw();
    requestAnimationFrame(loop);
  }
  reset();
  requestAnimationFrame(loop);
</script>
```

Phaser, for bigger 2D games with sprites, physics and scenes (draw sprites with
shapes or `data:` images):

```html
<!-- the style above -->
<div id="game"></div>
<script src="https://cdn.jsdelivr.net/npm/phaser@4.2.1/dist/phaser.min.js"></script>
<script>
  new Phaser.Game({
    type: Phaser.AUTO, width: 360, height: 240, parent: 'game', backgroundColor: '#1d3557',
    physics: { default: 'arcade', arcade: { gravity: { y: 300 } } },
    scene: {
      create() {
        const ball = this.add.circle(180, 40, 14, 0xffd166);
        this.physics.add.existing(ball);
        ball.body.setBounce(0.8).setCollideWorldBounds(true);
        this.input.on('pointerdown', () => ball.body.setVelocityY(-250));
      }
    }
  });
</script>
```

Matter.js, for physics toys (things that fall, stack and bounce):

```html
<!-- the style above -->
<div id="world"></div>
<script src="https://cdn.jsdelivr.net/npm/matter-js@0.20.0/build/matter.min.js"></script>
<script>
  const { Engine, Render, Runner, Bodies, Composite, Mouse, MouseConstraint } = Matter;
  const engine = Engine.create();
  const render = Render.create({ element: document.getElementById('world'), engine, options: { width: 360, height: 300, wireframes: false, background: 'transparent' } });
  Composite.add(engine.world, [
    Bodies.rectangle(180, 290, 360, 20, { isStatic: true }),
    ...Array.from({ length: 8 }, (_, i) => Bodies.circle(60 + i * 30, 20 * i, 14, { restitution: 0.7 }))
  ]);
  Composite.add(engine.world, MouseConstraint.create(engine, { mouse: Mouse.create(render.canvas) }));
  Render.run(render);
  Runner.run(Runner.create(), engine);
</script>
```

p5.js, for sketches, generative art and simple animations:

```html
<!-- the style above -->
<script src="https://cdn.jsdelivr.net/npm/p5@2.3.4/lib/p5.min.js"></script>
<script>
  function setup() { createCanvas(360, 240); noStroke(); }
  function draw() {
    background(27, 22, 24, 40);
    fill(242, 157, 185);
    circle(180 + 120 * sin(frameCount / 30), 120 + 60 * cos(frameCount / 20), 24);
  }
</script>
```

Three.js, for 3D (it loads as a module through an import map):

```html
<!-- the style above -->
<canvas id="scene" style="width: 100%; max-width: 640px; height: 360px; display: block"></canvas>
<script type="importmap">{ "imports": { "three": "https://cdn.jsdelivr.net/npm/three@0.186.1/build/three.module.js" } }</script>
<script type="module">
  import * as THREE from 'three';
  const canvas = document.getElementById('scene');
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setSize(canvas.clientWidth, canvas.clientHeight, false);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(60, canvas.clientWidth / canvas.clientHeight, 0.1, 100);
  camera.position.z = 3;
  const cube = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshNormalMaterial());
  scene.add(cube);
  renderer.setAnimationLoop(t => { cube.rotation.x = t / 1000; cube.rotation.y = t / 1300; renderer.render(scene, camera); });
</script>
```

Tone.js, for sound (a browser plays sound only after a click):

```html
<!-- the style above -->
<button id="play">Play a tune</button>
<script src="https://cdn.jsdelivr.net/npm/tone@15.1.22/build/Tone.js"></script>
<script>
  const synth = new Tone.Synth().toDestination();
  document.getElementById('play').addEventListener('click', async () => {
    await Tone.start();
    const now = Tone.now();
    ['C4', 'E4', 'G4', 'C5'].forEach((n, i) => synth.triggerAttackRelease(n, '8n', now + i * 0.25));
  });
</script>
```

## Apps and interfaces

React, with JSX through Babel (React 18; React 19 has no build that loads this way):

```html
<!-- the style above -->
<div id="root"></div>
<script src="https://unpkg.com/react@18.3.1/umd/react.production.min.js"></script>
<script src="https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js"></script>
<script src="https://unpkg.com/@babel/standalone@7.29.9/babel.min.js"></script>
<script type="text/babel">
  function Todo() {
    const [items, setItems] = React.useState([]);
    const [text, setText] = React.useState('');
    const add = e => { e.preventDefault(); if (text.trim()) setItems([...items, text.trim()]); setText(''); };
    return (
      <div className="card" style={{ maxWidth: 420 }}>
        <form onSubmit={add}>
          <input value={text} onChange={e => setText(e.target.value)} placeholder="Something to do" />
          <button>Add</button>
        </form>
        <ul>{items.map((t, i) => <li key={i} onClick={() => setItems(items.filter((_, j) => j !== i))}>{t}</li>)}</ul>
      </div>
    );
  }
  ReactDOM.createRoot(document.getElementById('root')).render(<Todo />);
</script>
```

Alpine.js, for a little behaviour right in the markup:

```html
<!-- the style above -->
<div class="card" x-data="{ count: 0 }" style="max-width: 240px">
  <button @click="count--">-</button>
  <strong x-text="count" style="margin: 0 12px"></strong>
  <button @click="count++">+</button>
</div>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.17.4/dist/cdn.min.js"></script>
```

Vue 3:

```html
<!-- the style above -->
<div id="app" class="card" style="max-width: 320px">
  <input v-model="name" placeholder="Your name">
  <p>Hello {{ name || 'there' }}, {{ name.length }} letters.</p>
</div>
<script src="https://unpkg.com/vue@3.5.43/dist/vue.global.prod.js"></script>
<script>
  Vue.createApp({ data: () => ({ name: '' }) }).mount('#app');
</script>
```

Tailwind, utility classes instead of a style block:

```html
<script src="https://cdn.tailwindcss.com/3.4.17"></script>
<script>tailwind.config = { darkMode: 'media' }</script>
<div class="min-h-screen p-6 bg-stone-50 text-stone-800 dark:bg-stone-900 dark:text-stone-100">
  <div class="max-w-sm mx-auto rounded-2xl p-5 bg-white dark:bg-stone-800 shadow">
    <h1 class="text-xl font-bold">Tailwind card</h1>
    <p class="mt-2 text-stone-500 dark:text-stone-400">Classes do the styling.</p>
    <button class="mt-4 px-4 py-2 rounded-lg bg-pink-500 text-white hover:bg-pink-600">Press</button>
  </div>
</div>
```

jQuery:

```html
<!-- the style above -->
<input id="item" placeholder="Add a line"> <button id="add">Add</button>
<ul id="list"></ul>
<script src="https://code.jquery.com/jquery-4.0.0.min.js"></script>
<script>
  $('#add').on('click', () => {
    const text = $('#item').val().trim();
    if (text) $('<li>').text(text).appendTo('#list');
    $('#item').val('').trigger('focus');
  });
</script>
```

## Maps, maths, icons, fonts

Leaflet draws a map from your own shapes and points. Map tiles come from other sites
and do not load here, so there is no street map underneath:

```html
<!-- the style above -->
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<div id="map" style="height: 360px; border-radius: 12px"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const map = L.map('map').setView([52.52, 13.40], 10);
  L.polygon([[52.6, 13.2], [52.6, 13.6], [52.4, 13.6], [52.4, 13.2]], { color: '#f29db9' }).addTo(map).bindTooltip('Berlin, roughly');
  L.marker([52.52, 13.40]).addTo(map).bindPopup('Centre');
</script>
```

KaTeX, for formulas:

```html
<!-- the style above -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.19.0/dist/katex.min.css">
<p id="f"></p>
<script src="https://cdn.jsdelivr.net/npm/katex@0.19.0/dist/katex.min.js"></script>
<script>
  katex.render('x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}', document.getElementById('f'), { displayMode: true });
</script>
```

Icons from Font Awesome, a font from Google Fonts:

```html
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.7.2/css/all.min.css">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap">
<!-- the style above -->
<p style="font-family: Inter, sans-serif"><i class="fa-solid fa-heart"></i> Made with Inter and Font Awesome</p>
```

Other libraries from the same hosts work too: marked for Markdown, anime.js for
animation, and more. A library that needs the network for its data (map tiles, a
weather service) gets nothing here.
