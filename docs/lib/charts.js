/**
 * charts.js — keeps every ECharts chart sized to its own box.
 *
 * Load right after echarts and before any script that draws a chart. It wraps
 * echarts.init, so every chart on the page is covered without the drawing
 * code doing anything.
 *
 * Charts used to resize on window "resize" alone, each module with its own
 * handler, and that failed three ways on phones:
 *   - one page-wide handler resized only the .echart-container and
 *     -tall classes, so -compact and -radar charts (the party chart among
 *     them) kept their first width after a rotation;
 *   - Safari fires "resize" during a rotation, before the new layout has
 *     settled, so a chart could resize to the old width and stay there;
 *   - a chart drawn while hidden measures 0×0, and nothing resized it when
 *     it was shown.
 * Watching each chart's own element with a ResizeObserver covers all three:
 * it fires when the element's laid-out size actually changes, for any reason.
 */
"use strict";

(() => {
  if (typeof echarts === "undefined") return;

  const tracked = new Set();
  const pending = new Set();
  let frame = 0;

  function flush() {
    frame = 0;
    for (const el of pending) {
      const chart = echarts.getInstanceByDom(el);
      if (!chart || chart.isDisposed()) {
        if (observer) observer.unobserve(el);
        tracked.delete(el);
        continue;
      }
      // A hidden chart has no size to take; the observer fires again once
      // it is shown, and it is sized then.
      if (!el.clientWidth || !el.clientHeight) continue;
      if (chart.getWidth() !== el.clientWidth || chart.getHeight() !== el.clientHeight) chart.resize();
    }
    pending.clear();
  }

  function schedule(el) {
    pending.add(el);
    if (!frame) frame = requestAnimationFrame(flush);
  }

  const observer = typeof ResizeObserver === "function"
    ? new ResizeObserver(entries => entries.forEach(e => schedule(e.target)))
    : null;

  const init = echarts.init;
  echarts.init = function (dom, ...rest) {
    const chart = init.call(this, dom, ...rest);
    if (dom && !tracked.has(dom)) {
      tracked.add(dom);
      if (observer) observer.observe(dom);
    }
    return chart;
  };

  // A safety net for rotation: check every chart now and again as the layout
  // settles. Charts already the right size are left alone, so this is cheap.
  function settle() {
    tracked.forEach(schedule);
    setTimeout(() => tracked.forEach(schedule), 250);
    setTimeout(() => tracked.forEach(schedule), 700);
  }
  window.addEventListener("resize", settle);
  window.addEventListener("orientationchange", settle);
  if (window.visualViewport) window.visualViewport.addEventListener("resize", settle);
})();
