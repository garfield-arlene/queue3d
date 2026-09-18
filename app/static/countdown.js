// Live "time remaining" countdown for a job that's printing, purely from
// the original estimate (released_at + duration_estimate_s, computed
// server-side - see jobs.printing_eta) - there's no live progress feed
// from the printer to correct this against once printing starts (see
// README.md's "Printer" to-do list). Deliberately labeled as an
// estimate, not a claim of real progress: the estimate is already known
// to run inaccurate in practice (per the user), so this counts down past
// zero into "over the estimate" rather than freezing or hiding at 0:00,
// which would read as more precise than it actually is.
//
// Runs as one page-wide interval, re-querying the DOM fresh every tick
// rather than caching element references - some of these elements live
// inside _jobs_table.html, which htmx replaces wholesale (outerHTML) on
// its own polling cycle, and a plain <script> tag inside swapped content
// doesn't re-run on that swap. Re-querying each tick means that doesn't
// matter: whatever [data-countdown-eta] elements exist right now just
// get updated, whether they're the original nodes or htmx's replacements.
(function () {
  function formatRemaining(ms) {
    const totalMin = Math.round(ms / 60000);
    if (totalMin > 1) return `~${totalMin} min remaining`;
    if (totalMin >= 0) return "any minute now";
    return `~${Math.abs(totalMin)} min over the estimate`;
  }

  function tick() {
    document.querySelectorAll("[data-countdown-eta]").forEach((el) => {
      const eta = new Date(el.dataset.countdownEta);
      el.textContent = formatRemaining(eta - Date.now());
    });
  }

  // This script loads in <head> (see base.html), before the body it
  // needs to query even exists yet - an immediate tick() here would just
  // find nothing and leave the estimate blank until the first interval
  // fires 15s later. Wait for DOMContentLoaded for the first real tick;
  // setInterval itself is safe to schedule immediately either way.
  document.addEventListener("DOMContentLoaded", tick);
  setInterval(tick, 15000);
})();
