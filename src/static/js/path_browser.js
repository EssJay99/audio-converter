// Filesystem folder browser for the output path field.
//
// Opens a modal that acts like a native folder picker:
//   - breadcrumb trail + Up/Home navigation
//   - double-click to enter a folder, single-click to select it
//   - a global search box that scans the whole machine (home, drives,
//     volumes) and jumps to matching folders, so you never have to browse
//     through every directory to find the one you want
//   - an optional "Native picker" button that uses the OS dialog (pywebview)

(function () {
    'use strict';

    function el(tag, attrs, text) {
        var node = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (k) {
                node.setAttribute(k, attrs[k]);
            });
        }
        if (text != null) node.textContent = text;
        return node;
    }

    function norm(p) {
        return String(p || '').replace(/[\\/]+$/, '');
    }

    function basename(p) {
        p = norm(p);
        var i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
        return i >= 0 ? p.slice(i + 1) : p;
    }

    function splitPath(p) {
        p = norm(p);
        var parts = p.split('/');
        if (parts.length === 1) parts = p.split('\\');
        return parts.filter(Boolean);
    }

    function debounce(fn, ms) {
        var timer = null;
        return function () {
            var args = arguments;
            clearTimeout(timer);
            timer = setTimeout(function () { fn.apply(null, args); }, ms);
        };
    }

    var modalCounter = 0;

    function loadDirs(path) {
        return fetch('/api/directories?q=' + encodeURIComponent(path) + '&depth=1')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                return { dirs: data.directories || [], path: data.base || path };
            });
    }

    function searchDirs(query) {
        return fetch('/api/directory-search?q=' + encodeURIComponent(query))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                return { dirs: data.directories || [], truncated: !!data.truncated };
            });
    }

    function openBrowser(input) {
        var id = 'path-browser-' + (++modalCounter);

        // ---------------- build the modal ----------------
        var overlay = el('div', { 'class': 'path-browser-overlay' });
        overlay.id = id;

        var box = el('div', { 'class': 'path-browser' });

        var head = el('div', { 'class': 'path-browser-head' });
        head.appendChild(el('h4', null, 'Choose a folder'));
        var closeBtn = el('button', { 'type': 'button', 'class': 'path-browser-close', 'aria-label': 'Close' }, '\u00d7');
        head.appendChild(closeBtn);

        var crumbBar = el('div', { 'class': 'path-browser-crumbs' });

        var searchWrap = el('div', { 'class': 'path-browser-search' });
        var search = el('input', {
            'type': 'text',
            'placeholder': 'Search your computer for a folder\u2026',
            'autocomplete': 'off',
            'spellcheck': 'false',
        });
        var searchHint = el('div', { 'class': 'path-browser-search-hint' },
            'Type 2+ characters to find matching folders anywhere on this machine.');
        searchWrap.appendChild(search);

        var toolbar = el('div', { 'class': 'path-browser-toolbar' });
        var upBtn = el('button', { 'type': 'button', 'class': 'btn btn-sm btn-outline-secondary' }, '\u2191 Up');
        var homeBtn = el('button', { 'type': 'button', 'class': 'btn btn-sm btn-outline-secondary' }, 'Home');
        toolbar.appendChild(upBtn);
        toolbar.appendChild(homeBtn);
        var nativeBtn = null;
        if (window.pywebview && window.pywebview.api &&
            typeof window.pywebview.api.pick_directory === 'function') {
            nativeBtn = el('button', { 'type': 'button', 'class': 'btn btn-sm btn-outline-primary ms-auto' }, 'Native picker');
            toolbar.appendChild(nativeBtn);
        }

        var resultInfo = el('div', { 'class': 'path-browser-result-info' });
        var body = el('div', { 'class': 'path-browser-body' });
        var empty = el('div', { 'class': 'path-browser-empty', 'hidden': true }, '');

        var foot = el('div', { 'class': 'path-browser-foot' });
        var currentPath = el('div', { 'class': 'path-browser-current' }, '');
        var selectBtn = el('button', {
            'type': 'button',
            'class': 'btn btn-primary',
            'disabled': 'disabled',
        }, 'Select this folder');
        var cancelBtn = el('button', { 'type': 'button', 'class': 'btn btn-outline-secondary' }, 'Cancel');
        foot.appendChild(currentPath);
        foot.appendChild(cancelBtn);
        foot.appendChild(selectBtn);

        box.appendChild(head);
        box.appendChild(crumbBar);
        box.appendChild(searchWrap);
        box.appendChild(searchHint);
        box.appendChild(resultInfo);
        box.appendChild(toolbar);
        box.appendChild(body);
        box.appendChild(empty);
        box.appendChild(foot);
        overlay.appendChild(box);

        document.body.appendChild(overlay);

        // Initial / Home location: the server-rendered default output path
        // (settings > convert form). Empty falls back to the OS home dir.
        var HOME = (typeof window.pathBrowserHome === 'string' && window.pathBrowserHome)
            ? window.pathBrowserHome : '';

        var state = {
            path: norm(input.value) || HOME,
            search: '',
            searchToken: 0,
            loading: false,
        };

        var active = -1;

        // ---------------- rendering ----------------

        function setPath(p) {
            state.path = norm(p) || HOME;
            clearSearch();
            render();
        }

        function clearSearch() {
            state.search = '';
            state.searchToken = 0;
            search.value = '';
            resultInfo.textContent = '';
        }

        function showLoad(message) {
            body.innerHTML = '';
            var row = el('div', { 'class': 'path-browser-loading' }, message);
            body.appendChild(row);
        }

        function renderListing() {
            var token = ++state.searchToken;
            currentPath.textContent = state.path;
            renderCrumbs(state.path);
            setSelectEnabled(pathIsValid(state.path));
            body.innerHTML = '';
            empty.hidden = true;
            resultInfo.textContent = '';

            showLoad('Loading\u2026');
            loadDirs(state.path).then(function (result) {
                if (token !== state.searchToken) return; // stale
                body.innerHTML = '';
                empty.textContent = 'This folder has no subfolders.';
                empty.hidden = result.dirs.length > 0;
                active = -1;

                result.dirs.forEach(function (dir) {
                    body.appendChild(makeRow(dir));
                });
            });
        }

        function renderSearch(query) {
            var token = ++state.searchToken;
            currentPath.textContent = state.path;
            renderCrumbs(state.path);
            body.innerHTML = '';
            empty.hidden = true;
            resultInfo.textContent = '';
            setSelectEnabled(false);

            showLoad('Searching\u2026 (first results can be slow on a big drive)');
            searchDirs(query).then(function (result) {
                if (token !== state.searchToken) return; // stale
                body.innerHTML = '';
                active = -1;

                if (result.dirs.length === 0) {
                    empty.textContent = 'No folders match "' + query + '".';
                    empty.hidden = false;
                    return;
                }

                resultInfo.textContent = result.truncated
                    ? 'Showing the first ' + result.dirs.length + ' matches out of many.'
                    : result.dirs.length + ' folder' + (result.dirs.length === 1 ? '' : 's') + ' match "' + query + '" \u2014 click one to go there.';
                result.dirs.forEach(function (dir) {
                    body.appendChild(makeResultRow(dir, query));
                });
            });
        }

        function render() {
            if (state.search.length >= 2) {
                renderSearch(state.search);
            } else {
                renderListing();
            }
        }

        // ---------------- rows ----------------

        function makeRow(dir) {
            var row = el('button', { 'type': 'button', 'class': 'path-browser-row' });
            row.setAttribute('data-dir', dir);
            row.appendChild(el('span', { 'class': 'path-browser-icon' }, '\u25b8'));
            row.appendChild(el('span', { 'class': 'path-browser-row-name' }, basename(dir)));
            row.appendChild(el('span', { 'class': 'path-browser-row-path' }, dir));
            row.addEventListener('click', function () { selectRow(dir); });
            row.addEventListener('dblclick', function () { setPath(dir); });
            return row;
        }

        function makeResultRow(dir, query) {
            var row = el('button', { 'type': 'button', 'class': 'path-browser-row path-browser-result' });
            row.setAttribute('data-dir', dir);
            row.appendChild(el('span', { 'class': 'path-browser-icon' }, '\u2315'));
            row.appendChild(el('span', { 'class': 'path-browser-row-name' }, basename(dir)));
            row.appendChild(el('span', { 'class': 'path-browser-row-path' }, dir));
            row.addEventListener('click', function () {
                // Jump into the found folder and show its contents.
                setPath(dir);
                selectRow(dir);
            });
            return row;
        }

        function pathIsValid(p) {
            return !!p;
        }

        function setSelectEnabled(enabled) {
            selectBtn.disabled = !enabled;
        }

        function renderCrumbs(path) {
            crumbBar.innerHTML = '';
            var parts = splitPath(path);

            // Filesystem root (drive letter on Windows, '/' on POSIX).
            var root = '/';
            if (/^[a-zA-Z]:[\\/]/.test(path)) {
                root = path.slice(0, 2) + '\\';
            }

            var rootCrumb = el('button', { 'type': 'button', 'class': 'path-crumb' }, root);
            rootCrumb.addEventListener('click', function () { setPath(root); });
            crumbBar.appendChild(rootCrumb);

            var accum = '';
            parts.forEach(function (part) {
                accum = accum ? accum + '/' + part : '/' + part;
                var crumb = el('button', { 'type': 'button', 'class': 'path-crumb' }, part);
                crumb.addEventListener('click', function () { setPath(accum); });
                crumbBar.appendChild(el('span', { 'class': 'path-crumb-sep' }, '/'));
                crumbBar.appendChild(crumb);
            });
        }

        // ---------------- events ----------------

        closeBtn.addEventListener('click', function () { close(); });
        cancelBtn.addEventListener('click', function () { close(); });
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) close();
        });
        document.addEventListener('keydown', function onKey(e) {
            if (e.key === 'Escape') {
                if (state.search.length >= 2) {
                    clearSearch();
                    render();
                } else {
                    close();
                }
                e.preventDefault();
            }
        });

        upBtn.addEventListener('click', function () {
            var parent = parentOf(state.path);
            if (parent) setPath(parent);
        });
        homeBtn.addEventListener('click', function () { setPath(HOME); });
        selectBtn.addEventListener('click', function () {
            input.value = state.path;
            close();
        });

        var debouncedSearch = debounce(function () {
            state.search = search.value.trim();
            render();
        }, 250);

        search.addEventListener('input', function () {
            if (search.value.trim().length === 0) {
                state.search = '';
                render();
            } else {
                debouncedSearch();
            }
        });

        search.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') {
                // Jump to the first result, if any.
                var first = body.querySelector('.path-browser-row[data-dir]');
                if (first) { first.click(); }
                e.preventDefault();
            }
            e.stopPropagation();
        });

        if (nativeBtn) {
            nativeBtn.addEventListener('click', function () {
                window.pywebview.api.pick_directory().then(function (p) {
                    if (p) { input.value = p; close(); }
                });
            });
        }

        function parentOf(p) {
            p = norm(p);
            var i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
            var parent = i >= 0 ? p.slice(0, i) : '';
            if (!parent) {
                return null;
            }
            return parent;
        }

        function close() {
            overlay.parentNode && overlay.parentNode.removeChild(overlay);
            document.removeEventListener('keydown', close);
        }

        // ---------------- boot ----------------
        selectBtn.focus();
        render();
    }

    function initAll() {
        var inputs = document.querySelectorAll('input[data-path-browser]');
        inputs.forEach(function (inp) {
            if (inp.getAttribute('data-path-browser-init')) return;
            inp.setAttribute('data-path-browser-init', '1');

            var wrap = el('div', { 'class': 'path-browser-input' });
            inp.parentNode.insertBefore(wrap, inp);
            wrap.appendChild(inp);
            var go = el('button', { 'type': 'button', 'class': 'btn btn-outline-secondary btn-sm' }, 'Browse\u2026');
            wrap.appendChild(go);
            go.addEventListener('click', function () { openBrowser(inp); });

            var copy = el('button', { 'type': 'button', 'class': 'btn btn-outline-secondary btn-sm' }, 'Copy Path');
            wrap.appendChild(copy);
            copy.addEventListener('click', function () { copyPath(); });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAll);
    } else {
        initAll();
    }
})();