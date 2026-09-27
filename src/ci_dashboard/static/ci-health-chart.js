// Initializes a uPlot chart in every ".ci-chart" container on the page.
// Each container holds a JSON payload (dates/series/threshold) in a
// <script type="application/json"> tag and an empty mount div - see
// render/health.py's chart_payload() for the data this reads.
(function () {
  "use strict";

  var THRESHOLD_COLOR = "#c7162b";

  function buildOpts(payload, width) {
    // uPlot indexes series/axes to the x-axis at slot 0; series[0] is a
    // required placeholder for it (defaults are fine) since payload.series
    // only describes the y-series.
    var series = [{}].concat(payload.series.map(function (s) {
      return {
        label: s.label,
        stroke: s.color,
        width: 1.6,
        points: { size: 4, fill: s.color, stroke: s.color },
      };
    }));

    var opts = {
      width: width,
      height: 220,
      // Extra right padding keeps the last point's marker and the trailing
      // x-axis tick label from clipping at the chart's edge.
      padding: [8, 70, null, null],
      series: series,
      scales: {
        x: { time: true },
        y: {
          // Ceiling is at least 2x the threshold so the dashed threshold
          // line (drawn in the hooks.draw block below) sits mid-chart
          // instead of pinned to the top edge; still expands to fit data
          // that exceeds it, and floors at 1 so an all-zero series isn't flat.
          range: function (u, initMin, initMax) {
            return [0, Math.max(initMax, (payload.threshold || 0) * 2, 1)];
          },
        },
      },
      axes: [
        {}, // x-axis (time); defaults are fine
        { values: function (u, vals) { return vals.map(function (v) { return v + "%"; }); } },
      ],
      legend: { show: true },
    };

    if (payload.threshold != null) {
      opts.hooks = {
        draw: [function (u) {
          var y = u.valToPos(payload.threshold, "y", true);
          if (y < u.bbox.top || y > u.bbox.top + u.bbox.height) return;
          var ctx = u.ctx;
          ctx.save();
          ctx.strokeStyle = THRESHOLD_COLOR;
          ctx.setLineDash([4, 4]);
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(u.bbox.left, y);
          ctx.lineTo(u.bbox.left + u.bbox.width, y);
          ctx.stroke();
          ctx.restore();
        }],
      };
    }

    return opts;
  }

  function initChart(container) {
    var dataEl = container.querySelector(".ci-chart__data");
    var mountEl = container.querySelector(".ci-chart__canvas");
    if (!dataEl || !mountEl) return;

    var payload = JSON.parse(dataEl.textContent);
    var timestamps = payload.dates.map(function (d) {
      return Date.parse(d + "T00:00:00Z") / 1000;
    });
    var data = [timestamps].concat(payload.series.map(function (s) { return s.values; }));
    var width = mountEl.clientWidth || 720;

    new uPlot(buildOpts(payload, width), data, mountEl);
  }

  document.querySelectorAll(".ci-chart").forEach(initChart);
})();
