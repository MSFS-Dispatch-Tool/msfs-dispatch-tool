/**
 * Cookie consent banner for VirtualDispatch's public pages.
 *
 * Strictly-necessary cookies (the login session, Supabase auth tokens)
 * don't require consent under GDPR/ePrivacy and are set regardless.
 * This banner exists for the optional category - analytics - which
 * isn't wired up yet but will be. The choice is stored in
 * localStorage (per-browser, never sent to the server) and exposed via
 * window.vdHasAnalyticsConsent() plus a 'vd-consent-changed' event, so
 * an analytics loader added later can gate itself on it without this
 * file needing to change.
 */
(function () {
  var STORAGE_KEY = 'vd_cookie_consent'; // 'accepted' | 'rejected'

  function getConsent() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (err) {
      return null;
    }
  }

  function setConsent(value) {
    try {
      localStorage.setItem(STORAGE_KEY, value);
    } catch (err) {
      // Private browsing / blocked storage - the banner will just
      // reappear next visit, which is an acceptable degradation.
    }
    document.dispatchEvent(new CustomEvent('vd-consent-changed', { detail: { consent: value } }));
  }

  window.vdHasAnalyticsConsent = function () {
    return getConsent() === 'accepted';
  };

  function hideBanner() {
    var el = document.getElementById('cookie-banner');
    if (el) el.classList.remove('visible');
  }

  document.addEventListener('DOMContentLoaded', function () {
    var banner = document.getElementById('cookie-banner');
    if (!banner) return;

    if (getConsent() === null) {
      banner.classList.add('visible');
    }

    var acceptBtn = document.getElementById('cookie-accept');
    var rejectBtn = document.getElementById('cookie-reject');
    if (acceptBtn) acceptBtn.addEventListener('click', function () { setConsent('accepted'); hideBanner(); });
    if (rejectBtn) rejectBtn.addEventListener('click', function () { setConsent('rejected'); hideBanner(); });
  });
})();
