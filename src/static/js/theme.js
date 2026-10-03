// Theme engine: mode (light/dark/system), accent color, density.
// Runs synchronously first in <head> so the saved theme applies before
// first paint (no flash). Persists in localStorage on this origin.
(function () {
    'use strict';

    var ACCENTS = ['blue', 'green', 'purple', 'orange', 'red'];
    var root = document.documentElement;

    function stored(key, fallback) {
        try {
            return localStorage.getItem(key) || fallback;
        } catch (err) {
            return fallback;
        }
    }

    function systemDark() {
        try {
            return window.matchMedia &&
                window.matchMedia('(prefers-color-scheme: dark)').matches;
        } catch (err) {
            return false;
        }
    }

    function effectiveMode() {
        var mode = stored('appThemeMode', 'light');
        if (mode === 'system') return systemDark() ? 'dark' : 'light';
        return mode === 'dark' ? 'dark' : 'light';
    }

    function apply() {
        root.setAttribute('data-theme', effectiveMode());
        var accent = stored('appThemeAccent', 'blue');
        if (ACCENTS.indexOf(accent) < 0) accent = 'blue';
        root.setAttribute('data-accent', accent);
        var density = stored('appThemeDensity', 'comfortable');
        root.setAttribute('data-density',
                          density === 'compact' ? 'compact' : 'comfortable');
    }

    function set(key, value) {
        try {
            localStorage.setItem(key, value);
        } catch (err) { /* private mode */ }
        apply();
        syncControls();
    }

    function syncControls() {
        var mode = stored('appThemeMode', 'light');
        var accent = stored('appThemeAccent', 'blue');
        var density = stored('appThemeDensity', 'comfortable');
        document.querySelectorAll('[data-theme-opt]').forEach(function (btn) {
            btn.classList.toggle('active', btn.dataset.themeOpt === mode);
        });
        document.querySelectorAll('[data-accent-opt]').forEach(function (btn) {
            btn.classList.toggle('active', btn.dataset.accentOpt === accent);
        });
        var compact = document.getElementById('densityCompact');
        var comfy = document.getElementById('densityComfy');
        if (compact) compact.checked = density === 'compact';
        if (comfy) comfy.checked = density !== 'compact';
    }

    apply();
    try {
        if (window.matchMedia) {
            window.matchMedia('(prefers-color-scheme: dark)')
                .addEventListener('change', apply);
        }
    } catch (err) { /* ignore */ }

    window.Theme = {
        setMode: function (m) { set('appThemeMode', m); },
        setAccent: function (a) { set('appThemeAccent', a); },
        setDensity: function (d) { set('appThemeDensity', d); },
        sync: syncControls,
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', syncControls);
    } else {
        syncControls();
    }
})();
