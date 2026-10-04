// Player tab: library grid, now-playing mirror, user playlists, visualizer.
// Drives the shared engine in player.js through window.AudioPlayer; the
// bottom bar stays the single owner of playback state.
(function () {
    'use strict';

    var library = [];
    var selectedIds = {};
    var userPlaylists = [];
    var lastCoverId = null;
    var lastSnapshotSig = '';
    var libShown = 100;

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function apiHeaders() {
        var token = '';
        try {
            // Shared helper from main.js (loaded on every player page).
            token = typeof csrfToken === 'function' ? csrfToken() : '';
        } catch (err) { /* ignore */ }
        return { 'Content-Type': 'application/json', 'X-CSRFToken': token };
    }

    function postJSON(url, payload) {
        return fetch(url, {
            method: 'POST',
            headers: apiHeaders(),
            body: JSON.stringify(payload || {}),
        }).then(function (r) { return r.json(); });
    }

    function stem(name) {
        return String(name || '').replace(/\.[^.]+$/, '');
    }

    function trackTitle(item) {
        return item.filename ? stem(item.filename) : (item.playlist_title || 'Track');
    }

    // ---------------------------------------------------------------- library

    function loadLibrary() {
        if (!document.getElementById('libGrid')) return;
        fetch('/api/library')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                library = data.items || [];
                renderLibrary();
            })
            .catch(function () {
                document.getElementById('libGrid').innerHTML =
                    '<span class="text-muted small">Library unavailable.</span>';
            });
    }

    function renderLibrary() {
        var grid = document.getElementById('libGrid');
        if (!grid) return;
        var q = '';
        var search = document.getElementById('tabSearch');
        if (search) q = search.value.trim().toLowerCase();
        var likedOnly = false;
        var likedBox = document.getElementById('tabLikedOnly');
        if (likedBox) likedOnly = likedBox.checked;
        var sort = 'recent';
        var sortSel = document.getElementById('tabSort');
        if (sortSel) sort = sortSel.value;
        var rows = library.filter(function (item) {
            if (likedOnly && !item.liked) return false;
            if (q && (trackTitle(item) + ' ' + (item.output_path || '') + ' ' +
                      (item.tag_title || '') + ' ' + (item.tag_artist || '') + ' ' +
                      (item.tag_album || '')).toLowerCase().indexOf(q) === -1) {
                return false;
            }
            return true;
        });
        if (sort === 'played') {
            rows.sort(function (a, b) { return (b.play_count || 0) - (a.play_count || 0); });
        } else if (sort === 'rated') {
            rows.sort(function (a, b) { return (b.rating || 0) - (a.rating || 0); });
        } else if (sort === 'duration') {
            rows.sort(function (a, b) { return (b.duration || 0) - (a.duration || 0); });
        } else if (sort === 'size') {
            rows.sort(function (a, b) { return (b.file_size || 0) - (a.file_size || 0); });
        } else if (sort === 'name') {
            rows.sort(function (a, b) {
                return trackTitle(a).localeCompare(trackTitle(b));
            });
        }
        grid.innerHTML = '';
        if (!rows.length) {
            if (!library.length && !q) {
                const empty = document.createElement('div');
                empty.className = 'lib-empty';
                const title = document.createElement('h5');
                title.textContent = 'Your library is empty';
                empty.appendChild(title);
                const text = document.createElement('p');
                text.className = 'text-muted small';
                text.textContent = 'Convert a link, adopt a folder of files you already own, or drop audio files right here.';
                empty.appendChild(text);
                const actions = document.createElement('div');
                actions.className = 'd-flex flex-wrap gap-2';
                const convert = document.createElement('a');
                convert.className = 'btn btn-primary';
                convert.href = '/';
                convert.textContent = 'Convert your first track';
                actions.appendChild(convert);
                const adopt = document.createElement('button');
                adopt.type = 'button';
                adopt.className = 'btn btn-outline-secondary';
                adopt.textContent = 'Adopt a folder instead';
                adopt.addEventListener('click', function () {
                    const form = document.getElementById('libAdoptForm');
                    if (form) {
                        form.classList.remove('d-none');
                        const input = document.getElementById('libAdoptPath');
                        if (input) input.focus();
                    }
                });
                actions.appendChild(adopt);
                empty.appendChild(actions);
                grid.appendChild(empty);
            } else {
                grid.innerHTML = '<span class="text-muted small">No tracks match.</span>';
            }
            return;
        }
        rows.slice(0, libShown).forEach(function (item) {
            var card = document.createElement('div');
            card.className = 'lib-card';
            var img = document.createElement('img');
            img.className = 'lib-cover';
            img.alt = '';
            img.loading = 'lazy';
            img.src = '/api/cover/' + item.id + '?size=thumb';
            img.onerror = function () { img.classList.add('d-none'); };
            img.style.cursor = 'zoom-in';
            img.title = 'Click to enlarge';
            img.addEventListener('click', function (ev) {
                ev.stopPropagation();
                if (window.AudioLightbox) {
                    window.AudioLightbox.open(img.src.replace('?size=thumb', ''));
                }
            });
            card.appendChild(img);
            var info = document.createElement('div');
            info.className = 'lib-info';
            var title = document.createElement('div');
            title.className = 'lib-title';
            title.textContent = trackTitle(item);
            title.title = trackTitle(item);
            info.appendChild(title);
            var sub = document.createElement('div');
            sub.className = 'lib-sub text-muted small';
            sub.textContent = item.format + (item.play_count ? ' · ×' + item.play_count : '');
            info.appendChild(sub);
            card.appendChild(info);
            var actions = document.createElement('div');
            actions.className = 'lib-actions';
            var select = document.createElement('input');
            select.type = 'checkbox';
            select.className = 'form-check-input lib-select';
            select.title = 'Select for bulk tagging';
            select.checked = !!selectedIds[item.id];
            select.setAttribute('data-select', item.id);
            select.addEventListener('change', function () {
                if (select.checked) selectedIds[item.id] = true;
                else delete selectedIds[item.id];
                renderBulkBar();
            });
            actions.appendChild(select);
            var play = document.createElement('button');
            play.type = 'button';
            play.className = 'btn btn-sm btn-primary';
            play.title = 'Play';
            play.setAttribute('data-play', item.id);
            play.textContent = '▶';
            actions.appendChild(play);
            var like = document.createElement('button');
            like.type = 'button';
            like.className = 'btn btn-sm btn-outline-secondary lib-like' + (item.liked ? ' liked' : '');
            like.title = 'Like';
            like.setAttribute('data-like', item.id);
            like.textContent = item.liked ? '♥' : '♡';
            actions.appendChild(like);
            var stars = document.createElement('span');
            stars.className = 'lib-stars';
            stars.title = 'Rate this track';
            for (var s = 1; s <= 5; s++) {
                (function (value) {
                    var star = document.createElement('button');
                    star.type = 'button';
                    star.className = 'lib-star' + (value <= (item.rating || 0) ? ' on' : '');
                    star.textContent = value <= (item.rating || 0) ? '★' : '☆';
                    star.setAttribute('data-rate', item.id + ':' + value);
                    star.title = value + ' star' + (value > 1 ? 's' : '');
                    stars.appendChild(star);
                })(s);
            }
            actions.appendChild(stars);
            if (userPlaylists.length) {
                var sel = document.createElement('select');
                sel.className = 'form-select form-select-sm lib-pl-select';
                sel.title = 'Add to playlist';
                var placeholder = document.createElement('option');
                placeholder.value = '';
                placeholder.textContent = '+ Playlist';
                sel.appendChild(placeholder);
                userPlaylists.forEach(function (pl) {
                    const opt = document.createElement('option');
                    opt.value = String(pl.id);
                    opt.textContent = pl.name;
                    sel.appendChild(opt);
                });
                sel.addEventListener('change', function () {
                    if (!sel.value) return;
                    postJSON('/api/playlists/' + sel.value + '/add', { conversion_id: item.id })
                        .then(function (res) {
                            if (typeof toast === 'function') {
                                toast(res.ok ? 'Added to playlist.' : (res.message || 'Could not add.'),
                                      res.ok ? 'success' : 'danger');
                            }
                            sel.value = '';
                            loadUserPlaylists();
                        })
                        .catch(function () {
                            if (typeof toast === 'function') toast('Could not add.', 'danger');
                            sel.value = '';
                        });
                });
                actions.appendChild(sel);
            }
            card.appendChild(actions);
            grid.appendChild(card);
        });
        if (rows.length > libShown) {
            const more = document.createElement('button');
            more.type = 'button';
            more.className = 'btn btn-sm btn-outline-secondary mt-2';
            more.textContent = 'Show more (' + (rows.length - libShown) + ' remaining)';
            more.addEventListener('click', function () {
                libShown += 100;
                renderLibrary();
            });
            grid.appendChild(more);
        }
    }

    // ------------------------------------------------------------- smart mixes

    var SMART_MIXES = [
        { key: 'played', label: 'Most played' },
        { key: 'recent', label: 'Recently added' },
        { key: 'unplayed', label: 'Unplayed' },
        { key: 'liked', label: 'Liked' },
        { key: 'rated', label: 'Top rated' },
    ];

    function loadSmartMixes() {
        var box = document.getElementById('tabSmart');
        if (!box) return;
        box.innerHTML = '';
        SMART_MIXES.forEach(function (mix) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-sm btn-outline-primary';
            btn.textContent = '\u25B6 ' + mix.label;
            btn.addEventListener('click', function () {
                fetch('/api/smart/' + mix.key)
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        var items = (data.items || []).filter(function (it) {
                            return it.file_exists;
                        });
                        if (!items.length) {
                            if (typeof toast === 'function') toast('Nothing in this mix yet.', 'info');
                            return;
                        }
                        if (window.AudioPlayer) {
                            window.AudioPlayer.playItems(items, 0, mix.label);
                        }
                    })
                    .catch(function () {
                        if (typeof toast === 'function') toast('Could not load this mix.', 'danger');
                    });
            });
            box.appendChild(btn);
        });
    }

    // ------------------------------------------------------------- groups

    var libView = 'tracks';

    function fmtDur(seconds) {
        seconds = Math.max(0, Math.round(Number(seconds) || 0));
        var h = Math.floor(seconds / 3600);
        var m = Math.floor((seconds % 3600) / 60);
        return h ? h + 'h ' + m + 'm' : m + 'm';
    }

    function fmtFolderBytes(bytes) {
        bytes = Math.max(0, Number(bytes) || 0);
        if (bytes < 1024) return bytes + ' B';
        var units = ['KB', 'MB', 'GB'];
        var u = -1;
        do {
            bytes /= 1024;
            u += 1;
        } while (bytes >= 1024 && u < units.length - 1);
        return bytes.toFixed(1) + ' ' + units[u];
    }

    function renderFolderView() {
        var grid = document.getElementById('libGrid');
        var groups = document.getElementById('libGroups');
        if (!grid || !groups) return;
        grid.classList.add('d-none');
        groups.classList.remove('d-none');
        groups.innerHTML = '';
        var loading = document.createElement('span');
        loading.className = 'text-muted small';
        loading.textContent = 'Reading folders…';
        groups.appendChild(loading);
        fetch('/api/folders')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                groups.innerHTML = '';
                if (!data.ok || !data.tree) {
                    groups.innerHTML = '<span class="text-muted small">Could not read folders.</span>';
                    return;
                }
                groups.appendChild(renderFolderNode(data.tree, true));
            })
            .catch(function () {
                groups.innerHTML = '<span class="text-muted small">Could not read folders.</span>';
            });
    }

    function renderFolderNode(node, isRoot) {
        var wrap = document.createElement('div');
        wrap.className = 'lib-folder';
        var details = document.createElement('details');
        if (isRoot) details.open = true;
        var summary = document.createElement('summary');
        summary.className = 'lib-folder-head';
        var fname = document.createElement('strong');
        fname.textContent = node.name;
        summary.appendChild(fname);
        var meta = document.createElement('span');
        meta.className = 'text-muted small ms-2';
        meta.textContent = node.tracks + ' track' + (node.tracks === 1 ? '' : 's') +
            ' · ' + fmtFolderBytes(node.bytes);
        summary.appendChild(meta);
        details.appendChild(summary);
        (node.dirs || []).forEach(function (child) {
            details.appendChild(renderFolderNode(child, false));
        });
        (node.files || []).forEach(function (f) {
            var row = document.createElement('div');
            row.className = 'lib-file';
            var label = document.createElement('span');
            label.className = 'text-truncate';
            label.textContent = f.name;
            label.title = f.path;
            row.appendChild(label);
            var sub = document.createElement('span');
            sub.className = 'text-muted small ms-2';
            var bits = [fmtFolderBytes(f.size)];
            if (f.duration) {
                var m = Math.floor(f.duration / 60);
                var s = Math.floor(f.duration % 60);
                bits.push(m + ':' + String(s).padStart(2, '0'));
            }
            sub.textContent = bits.join(' · ');
            row.appendChild(sub);
            if (f.id) {
                var btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'btn btn-sm btn-outline-secondary ms-2';
                btn.textContent = '\u25B6';
                btn.title = 'Play';
                btn.addEventListener('click', function () {
                    fetch('/api/track/' + f.id)
                        .then(function (r) { return r.json(); })
                        .then(function (data) {
                            if (data.ok && window.AudioPlayer) {
                                window.AudioPlayer.playItems([data.item], 0, node.name);
                            }
                        })
                        .catch(function () {});
                });
                row.appendChild(btn);
            } else {
                var hint = document.createElement('span');
                hint.className = 'text-muted small ms-2';
                hint.textContent = 'untracked';
                hint.title = 'Adopt this folder to track it';
                row.appendChild(hint);
            }
            details.appendChild(row);
        });
        wrap.appendChild(details);
        return wrap;
    }

    function renderGroups(kind) {
        var grid = document.getElementById('libGrid');
        var groups = document.getElementById('libGroups');
        if (!grid || !groups) return;
        grid.classList.add('d-none');
        groups.classList.remove('d-none');
        groups.innerHTML = '';
        var loading = document.createElement('span');
        loading.className = 'text-muted small';
        loading.textContent = 'Loading…';
        groups.appendChild(loading);
        fetch(kind === 'albums' ? '/api/albums' : '/api/artists')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var list = data.albums || data.artists || [];
                groups.innerHTML = '';
                if (!list.length) {
                    groups.innerHTML = '<span class="text-muted small">Nothing here yet.</span>';
                    return;
                }
                list.forEach(function (g) {
                    var card = document.createElement('button');
                    card.type = 'button';
                    card.className = 'lib-group-card';
                    var img = document.createElement('img');
                    img.className = 'lib-cover';
                    img.alt = '';
                    img.loading = 'lazy';
                    img.src = '/api/cover/' + g.cover_id + '?size=thumb';
                    img.onerror = function () { img.classList.add('d-none'); };
                    card.appendChild(img);
                    var info = document.createElement('div');
                    info.className = 'lib-info';
                    var name = document.createElement('div');
                    name.className = 'lib-title';
                    name.textContent = g.name;
                    info.appendChild(name);
                    var sub = document.createElement('div');
                    sub.className = 'lib-sub text-muted small';
                    sub.textContent = g.tracks + ' track' + (g.tracks === 1 ? '' : 's') + ' · ' + fmtDur(g.seconds);
                    info.appendChild(sub);
                    card.appendChild(info);
                    card.addEventListener('click', function () {
                        drillGroup(kind, g.name);
                    });
                    groups.appendChild(card);
                });
            })
            .catch(function () {
                groups.innerHTML = '<span class="text-muted small">Could not load.</span>';
            });
    }

    function drillGroup(kind, name) {
        var groups = document.getElementById('libGroups');
        if (!groups) return;
        var by = kind === 'albums' ? 'album' : 'artist';
        fetch('/api/album/tracks?name=' + encodeURIComponent(name) + '&by=' + by)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var items = (data.items || []).filter(function (it) { return it.file_exists; });
                groups.innerHTML = '';
                var back = document.createElement('button');
                back.type = 'button';
                back.className = 'btn btn-sm btn-outline-secondary mb-2';
                back.textContent = '\u2190 All ' + kind;
                back.addEventListener('click', function () { renderGroups(kind); });
                groups.appendChild(back);
                if (!items.length) {
                    var none = document.createElement('div');
                    none.className = 'text-muted small';
                    none.textContent = 'No playable tracks.';
                    groups.appendChild(none);
                    return;
                }
                var playAll = document.createElement('button');
                playAll.type = 'button';
                playAll.className = 'btn btn-sm btn-primary mb-2 ms-2';
                playAll.textContent = '\u25B6 Play all (' + items.length + ')';
                playAll.addEventListener('click', function () {
                    if (window.AudioPlayer) window.AudioPlayer.playItems(items, 0, name);
                });
                groups.appendChild(playAll);
                var list = document.createElement('ul');
                list.className = 'list-group list-group-flush tab-queue';
                items.forEach(function (it, idx) {
                    var li = document.createElement('li');
                    li.className = 'list-group-item d-flex justify-content-between align-items-center';
                    var label = document.createElement('span');
                    label.className = 'text-truncate';
                    label.textContent = (idx + 1) + '. ' + trackTitle(it);
                    li.appendChild(label);
                    var btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'btn btn-sm btn-outline-secondary';
                    btn.textContent = '\u25B6';
                    btn.addEventListener('click', function () {
                        if (window.AudioPlayer) window.AudioPlayer.playItems(items, idx, name);
                    });
                    li.appendChild(btn);
                    list.appendChild(li);
                });
                groups.appendChild(list);
            })
            .catch(function () {});
    }

    function setLibView(view) {
        libView = view;
        if (view === 'folders') {
            renderFolderView();
            ['Tracks', 'Albums', 'Artists', 'Folders'].forEach(function (v) {
                var b = document.getElementById('libView' + v);
                if (b) b.classList.toggle('active', v.toLowerCase() === view);
            });
            return;
        }
        ['Tracks', 'Albums', 'Artists', 'Folders'].forEach(function (v) {
            var btn = document.getElementById('libView' + v);
            if (btn) btn.classList.toggle('active', v.toLowerCase() === view);
        });
        var grid = document.getElementById('libGrid');
        var groups = document.getElementById('libGroups');
        if (view === 'tracks') {
            if (groups) groups.classList.add('d-none');
            if (grid) grid.classList.remove('d-none');
            renderLibrary();
        } else {
            renderGroups(view);
        }
    }

    // ------------------------------------------------------------- library tools

    function selectedList() {
        return Object.keys(selectedIds).map(Number).filter(function (id) {
            return library.some(function (it) { return it.id === id; });
        });
    }

    function renderBulkBar() {
        var bar = document.getElementById('libBulkBar');
        var count = document.getElementById('libBulkCount');
        if (!bar || !count) return;
        var n = selectedList().length;
        bar.classList.toggle('d-none', n === 0);
        count.textContent = n + ' selected';
    }

    function initLibraryTools() {
        var adoptBtn = document.getElementById('libAdoptBtn');
        var adoptForm = document.getElementById('libAdoptForm');
        if (adoptBtn && adoptForm) {
            adoptBtn.addEventListener('click', function () {
                adoptForm.classList.toggle('d-none');
            });
            adoptForm.addEventListener('submit', function (e) {
                e.preventDefault();
                var input = document.getElementById('libAdoptPath');
                var path = input ? input.value.trim() : '';
                if (!path) return;
                postJSON('/api/adopt', { path: path })
                    .then(function (res) {
                        if (typeof toast === 'function') {
                            toast(res.ok
                                ? ('Adopted ' + res.added + ' file(s)' +
                                   (res.skipped ? ', ' + res.skipped + ' already tracked' : '') +
                                   (res.truncated ? ' (capped — run again for the rest)' : '') + '.')
                                : (res.message || 'Could not adopt.'), res.ok ? 'success' : 'danger');
                        }
                        if (res.ok) loadLibrary();
                    })
                    .catch(function () {
                        if (typeof toast === 'function') toast('Could not adopt.', 'danger');
                    });
            });
        }
        var tidyBtn = document.getElementById('libTidyBtn');
        if (tidyBtn) {
            tidyBtn.addEventListener('click', function () {
                if (!confirm('Move library files into Artist / Album folders from their tags?')) return;
                postJSON('/api/tidy', {})
                    .then(function (res) {
                        if (typeof toast === 'function') {
                            toast(res.ok
                                ? ('Moved ' + res.moved + ' file(s)' +
                                   (res.skipped ? ', ' + res.skipped + ' skipped (no tags)' : '') + '.')
                                : (res.message || 'Could not tidy.'), res.ok ? 'success' : 'danger');
                        }
                        if (res.ok) loadLibrary();
                    })
                    .catch(function () {
                        if (typeof toast === 'function') toast('Could not tidy.', 'danger');
                    });
            });
        }
        var coversBtn = document.getElementById('libCoversBtn');
        if (coversBtn) {
            coversBtn.addEventListener('click', function () {
                postJSON('/api/covers/backfill', {})
                    .then(function (res) {
                        if (typeof toast === 'function') {
                            toast(res.ok
                                ? ('Covers: ' + res.filled + ' fetched, ' + res.has_art + ' already had art' +
                                   (res.missing_url ? ', ' + res.missing_url + ' without source link' : '') + '.')
                                : (res.message || 'Could not fetch covers.'), res.ok ? 'success' : 'danger');
                        }
                        if (res.ok && res.filled) loadLibrary();
                    })
                    .catch(function () {
                        if (typeof toast === 'function') toast('Could not fetch covers.', 'danger');
                    });
            });
        }
        var apply = document.getElementById('libBulkApply');
        if (apply) {
            apply.addEventListener('click', function () {
                var ids = selectedList();
                var artist = document.getElementById('libBulkArtist');
                var album = document.getElementById('libBulkAlbum');
                var title = document.getElementById('libBulkTitle');
                postJSON('/api/tags/bulk', {
                    ids: ids,
                    artist: artist ? artist.value : '',
                    album: album ? album.value : '',
                    title: title ? title.value : '',
                })
                    .then(function (res) {
                        if (typeof toast === 'function') {
                            toast(res.ok
                                ? ('Tagged ' + res.updated + ' track(s)' +
                                   (res.failed ? ', ' + res.failed + ' failed' : '') + '.')
                                : (res.message || 'Could not write tags.'), res.ok ? 'success' : 'danger');
                        }
                        if (res.ok) {
                            selectedIds = {};
                            renderBulkBar();
                            loadLibrary();
                        }
                    })
                    .catch(function () {
                        if (typeof toast === 'function') toast('Could not write tags.', 'danger');
                    });
            });
        }
        var clear = document.getElementById('libBulkClear');
        if (clear) {
            clear.addEventListener('click', function () {
                selectedIds = {};
                renderBulkBar();
                renderLibrary();
            });
        }
        initDropImport();
    }

    var AUDIO_EXTS = ['flac', 'm4a', 'alac', 'wav', 'ogg', 'opus', 'mp3',
                      'mp4', 'webm', 'mkv', 'mov', 'avi'];

    function initDropImport() {
        var grid = document.getElementById('libGrid');
        if (!grid || grid.dataset.dropInit) return;
        grid.dataset.dropInit = '1';
        ['dragenter', 'dragover'].forEach(function (ev) {
            grid.addEventListener(ev, function (e) {
                e.preventDefault();
                grid.classList.add('lib-drop');
            });
        });
        ['dragleave', 'drop'].forEach(function (ev) {
            grid.addEventListener(ev, function (e) {
                e.preventDefault();
                grid.classList.remove('lib-drop');
            });
        });
        grid.addEventListener('drop', function (e) {
            var files = (e.dataTransfer && e.dataTransfer.files) || [];
            if (!files.length) return;
            var lists = [];
            var uploads = [];
            Array.from(files).forEach(function (f) {
                var name = f.name || '';
                var ext = name.split('.').pop().toLowerCase();
                if (ext === 'm3u' || ext === 'm3u8') lists.push(f);
                else if (AUDIO_EXTS.indexOf(ext) >= 0) uploads.push(f);
            });
            lists.forEach(function (f) {
                if (window.AudioPlayer && window.AudioPlayer.importFile) {
                    window.AudioPlayer.importFile(f);
                }
            });
            if (uploads.length) uploadFiles(uploads);
            if (!lists.length && !uploads.length && typeof toast === 'function') {
                toast('Drop audio files or .m3u playlists.', 'info');
            }
        });
    }

    function uploadFiles(files) {
        var form = new FormData();
        Array.from(files).slice(0, 50).forEach(function (f) {
            form.append('files', f, f.name);
        });
        var token = '';
        try {
            token = typeof csrfToken === 'function' ? csrfToken() : '';
        } catch (err) {}
        if (token) form.append('csrf_token', token);
        fetch('/api/upload', { method: 'POST', body: form })
            .then(function (r) { return r.json(); })
            .then(function (res) {
                if (typeof toast === 'function') {
                    toast(res.ok
                        ? ('Imported ' + res.added + ' file(s)' +
                           (res.skipped && res.skipped.length ? ', skipped: ' + res.skipped.join(', ') : '') + '.')
                        : (res.message || 'Could not import.'), res.ok ? 'success' : 'danger');
                }
                if (res.ok) loadLibrary();
            })
            .catch(function () {
                if (typeof toast === 'function') toast('Could not import.', 'danger');
            });
    }

    // ------------------------------------------------------------- playlists

    function loadUserPlaylists() {
        if (!document.getElementById('tabPlaylists')) return;
        fetch('/api/playlists')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                userPlaylists = data.items || [];
                renderUserPlaylists();
                renderLibrary();
            })
            .catch(function () {});
    }

    function renderUserPlaylists() {
        const box = document.getElementById('tabPlaylists');
        if (!box) return;
        box.innerHTML = '';
        if (!userPlaylists.length) {
            box.innerHTML = '<span class="text-muted small">No playlists yet.</span>';
            return;
        }
        userPlaylists.forEach(function (pl) {
            const wrap = document.createElement('div');
            wrap.className = 'user-pl';
            const head = document.createElement('div');
            head.className = 'user-pl-head';
            const name = document.createElement('strong');
            name.textContent = pl.name + ' (' + pl.count + ')';
            head.appendChild(name);
            const mkBtn = function (label, title, fn) {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'btn btn-sm btn-outline-secondary';
                btn.textContent = label;
                btn.title = title;
                btn.addEventListener('click', fn);
                head.appendChild(btn);
                return btn;
            };
            mkBtn('▶ Play', 'Play this playlist', function () {
                fetch('/api/playlists/' + pl.id)
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        const playable = (data.items || []).filter(function (it) {
                            return it.file_exists && (it.status === 'completed' || it.status === 'skipped');
                        });
                        if (!playable.length) {
                            if (typeof toast === 'function') toast('Nothing playable in here.', 'info');
                            return;
                        }
                        if (window.AudioPlayer) {
                            window.AudioPlayer.playItems(playable, 0, pl.name);
                        }
                    })
                    .catch(function () {});
            });
            mkBtn('Delete', 'Delete playlist (files kept)', function () {
                if (!confirm('Delete "' + pl.name + '"? Files are kept.')) return;
                postJSON('/api/playlists/' + pl.id + '/delete', {})
                    .then(function () { loadUserPlaylists(); })
                    .catch(function () {});
            });
            wrap.appendChild(head);
            const list = document.createElement('ul');
            list.className = 'user-pl-tracks';
            (pl.tracks || []).forEach(function (entry, idx, arr) {
                const li = document.createElement('li');
                const jump = document.createElement('button');
                jump.type = 'button';
                jump.className = 'user-pl-jump';
                jump.textContent = (idx + 1) + '. ' + (entry.title || ('Track ' + entry.conversion_id));
                jump.title = 'Play from here';
                jump.addEventListener('click', function () {
                    fetch('/api/playlists/' + pl.id)
                        .then(function (r) { return r.json(); })
                        .then(function (data) {
                            const playable = (data.items || []).filter(function (it) {
                                return it.file_exists && (it.status === 'completed' || it.status === 'skipped');
                            });
                            const start = Math.max(0, playable.findIndex(function (it) {
                                return it.id === entry.conversion_id;
                            }));
                            if (window.AudioPlayer) {
                                window.AudioPlayer.playItems(playable, start, pl.name);
                            }
                        })
                        .catch(function () {});
                });
                li.appendChild(jump);
                const up = document.createElement('button');
                up.type = 'button';
                up.className = 'btn btn-sm btn-link user-pl-move';
                up.textContent = '↑';
                up.title = 'Move up';
                up.addEventListener('click', function () {
                    moveEntry(pl.id, entry.item_id, 'up');
                });
                li.appendChild(up);
                const down = document.createElement('button');
                down.type = 'button';
                down.className = 'btn btn-sm btn-link user-pl-move';
                down.textContent = '↓';
                down.title = 'Move down';
                down.addEventListener('click', function () {
                    moveEntry(pl.id, entry.item_id, 'down');
                });
                li.appendChild(down);
                const rm = document.createElement('button');
                rm.type = 'button';
                rm.className = 'btn btn-sm btn-link user-pl-remove';
                rm.textContent = '×';
                rm.title = 'Remove (file kept)';
                rm.addEventListener('click', function () {
                    postJSON('/api/playlists/' + pl.id + '/remove', { item_id: entry.item_id })
                        .then(function () { loadUserPlaylists(); })
                        .catch(function () {});
                });
                li.appendChild(rm);
                list.appendChild(li);
            });
            wrap.appendChild(list);
            box.appendChild(wrap);
        });
    }

    function moveEntry(pid, itemId, direction) {
        postJSON('/api/playlists/' + pid + '/move', { item_id: itemId, direction: direction })
            .then(function () { loadUserPlaylists(); })
            .catch(function () {});
    }

    // ------------------------------------------------------------ now playing

    function syncNowPlaying() {
        if (!window.AudioPlayer || !document.getElementById('tabTitle')) return;
        const snap = window.AudioPlayer.snapshot();
        const sig = snap.queue.length + ':' + snap.index + ':' + snap.context;
        if (sig === lastSnapshotSig) return;
        lastSnapshotSig = sig;
        const current = snap.queue[snap.index];
        document.getElementById('tabTitle').textContent = (current && current.display) || 'Nothing playing';
        document.getElementById('tabArtist').textContent = snap.context || '';
        const cover = document.getElementById('tabCover');
        if (current && current.id !== lastCoverId) {
            lastCoverId = current.id;
            cover.src = '/api/cover/' + current.id;
            cover.classList.remove('d-none');
            cover.onerror = function () { cover.classList.add('d-none'); };
        } else if (!current) {
            lastCoverId = null;
            cover.classList.add('d-none');
            cover.removeAttribute('src');
        }
        const list = document.getElementById('tabQueue');
        list.innerHTML = '';
        snap.queue.forEach(function (item, i) {
            const li = document.createElement('li');
            li.className = 'list-group-item tab-queue-item' + (i === snap.index ? ' active' : '');
            li.textContent = (i + 1) + '. ' + item.display;
            li.title = 'Play from here';
            li.addEventListener('click', function () {
                window.AudioPlayer.playIndex(i);
            });
            list.appendChild(li);
        });
    }

    // ------------------------------------------------------------ visualizer

    let vizRaf = null;

    function drawViz() {
        vizRaf = null;
        const canvas = document.getElementById('viz');
        if (!canvas) return;
        const analyser = window.AudioPlayer ? window.AudioPlayer.getAnalyser() : null;
        // Only burn frames while there is something to draw: playing audio,
        // a live analyser, and a visible tab. The play listener restarts us.
        if (!analyser || !tabPlaying || document.hidden || !canvas.offsetParent) {
            return;
        }
        const ctx2d = canvas.getContext('2d');
        const W = canvas.width;
        const H = canvas.height;
        ctx2d.clearRect(0, 0, W, H);
        if (!analyser) {
            scheduleViz();
            return;
        }
        const data = new Uint8Array(analyser.frequencyBinCount);
        analyser.getByteFrequencyData(data);
        const bars = 48;
        const step = Math.max(1, Math.floor(data.length / bars));
        const gap = 2;
        const bw = (W - gap * (bars - 1)) / bars;
        ctx2d.fillStyle = '#3498db';
        for (let i = 0; i < bars; i++) {
            const v = data[i * step] / 255;
            const h = Math.max(2, v * H);
            ctx2d.fillRect(i * (bw + gap), H - h, bw, h);
        }
        scheduleViz();
    }

    function scheduleViz() {
        if (vizRaf === null && document.getElementById('viz')) {
            vizRaf = requestAnimationFrame(function () {
                vizRaf = null;
                drawViz();
            });
        }
    }

    // ------------------------------------------------- recent + dupes ----

    function loadRecent() {
        const list = document.getElementById('tabRecent');
        if (!list) return;
        fetch('/api/recently-played')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                const items = data.items || [];
                list.innerHTML = '';
                if (!items.length) {
                    list.innerHTML = '<span class="text-muted small">Play something first.</span>';
                    return;
                }
                items.forEach(function (item, i) {
                    const li = document.createElement('li');
                    li.className = 'list-group-item tab-queue-item';
                    li.textContent = (i + 1) + '. ' + trackTitle(item);
                    li.title = 'Play';
                    li.addEventListener('click', function () {
                        if (window.AudioPlayer) {
                            const playable = items.filter(function (it) { return it.file_exists; });
                            const start = Math.max(0, playable.findIndex(function (it) { return it.id === item.id; }));
                            window.AudioPlayer.playItems(playable, start, 'Recently played');
                        }
                    });
                    list.appendChild(li);
                });
            })
            .catch(function () {});
    }

    function loadDupes() {
        const box = document.getElementById('tabDupes');
        if (!box) return;
        fetch('/api/duplicates')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                const groups = data.groups || [];
                box.innerHTML = '';
                if (!groups.length) {
                    box.innerHTML = '<span class="text-muted small">No duplicates found.</span>';
                    return;
                }
                groups.forEach(function (group) {
                    const wrap = document.createElement('div');
                    wrap.className = 'user-pl';
                    const head = document.createElement('div');
                    head.className = 'user-pl-head';
                    const name = document.createElement('strong');
                    name.textContent = group.title + ' (' + group.items.length + ')';
                    head.appendChild(name);
                    wrap.appendChild(head);
                    const list = document.createElement('ul');
                    list.className = 'user-pl-tracks';
                    group.items.forEach(function (item) {
                        const li = document.createElement('li');
                        const label = document.createElement('span');
                        label.className = 'user-pl-jump';
                        label.textContent = item.output_path;
                        label.title = item.output_path;
                        li.appendChild(label);
                        const rm = document.createElement('button');
                        rm.type = 'button';
                        rm.className = 'btn btn-sm btn-link user-pl-remove';
                        rm.textContent = 'Delete';
                        rm.title = 'Delete file from disk';
                        rm.addEventListener('click', function () {
                            if (!confirm('Move this file to the Trash?')) return;
                            postJSON('/api/delete/' + item.id, {})
                                .then(function (res) {
                                    if (typeof toast === 'function') {
                                        toast(res.ok ? (res.message || 'Deleted.') : (res.message || 'Could not delete.'),
                                              res.ok ? 'success' : 'danger');
                                    }
                                    loadDupes();
                                    loadLibrary();
                                })
                                .catch(function () {});
                        });
                        li.appendChild(rm);
                        list.appendChild(li);
                    });
                    wrap.appendChild(list);
                    box.appendChild(wrap);
                });
            })
            .catch(function () {
                box.innerHTML = '<span class="text-muted small">Could not scan.</span>';
            });
    }

    // ------------------------------------------------------------------ init

    var tabPlaying = false;

    function wirePlaybackState() {
        const audio = document.getElementById('appPlayerAudio');
        if (!audio) return;
        audio.addEventListener('play', function () {
            tabPlaying = true;
            scheduleViz();
        });
        audio.addEventListener('pause', function () { tabPlaying = false; });
    }

    function init() {
        if (!document.getElementById('libGrid')) return;
        loadUserPlaylists();
        loadLibrary();
        loadRecent();
        loadDupes();
        loadStats();
        loadSmartMixes();
        initLibraryTools();
        ['Tracks', 'Albums', 'Artists', 'Folders'].forEach(function (v) {
            var btn = document.getElementById('libView' + v);
            if (btn) {
                btn.addEventListener('click', function () { setLibView(v.toLowerCase()); });
            }
        });
        var recentClear = document.getElementById('tabRecentClear');
        if (recentClear) {
            recentClear.addEventListener('click', function () {
                if (!confirm('Forget when tracks were played? Play counts stay.')) return;
                postJSON('/api/recently-played/clear', {})
                    .then(function () { loadRecent(); })
                    .catch(function () {});
            });
        }
        wirePlaybackState();
        document.addEventListener('trackplayed', function () {
            setTimeout(loadRecent, 1500);
        });
        const search = document.getElementById('tabSearch');
        if (search) search.addEventListener('input', function () { libShown = 100; renderLibrary(); });
        const liked = document.getElementById('tabLikedOnly');
        if (liked) liked.addEventListener('change', function () { libShown = 100; renderLibrary(); });
        const sort = document.getElementById('tabSort');
        if (sort) sort.addEventListener('change', function () { libShown = 100; renderLibrary(); });
        const saveQueueForm = document.getElementById('tabSaveQueue');
        if (saveQueueForm) {
            saveQueueForm.addEventListener('submit', function (e) {
                e.preventDefault();
                const nameInput = document.getElementById('tabSaveQueueName');
                const name = nameInput ? nameInput.value.trim() : '';
                if (!name) return;
                if (!window.AudioPlayer || !window.AudioPlayer.snapshot) {
                    if (typeof toast === 'function') toast('Player is not ready.', 'danger');
                    return;
                }
                const snap = window.AudioPlayer.snapshot();
                if (!snap.queue.length) {
                    if (typeof toast === 'function') toast('The queue is empty.', 'info');
                    return;
                }
                postJSON('/api/playlists', { name: name })
                    .then(function (res) {
                        if (!res.ok || !res.playlist) throw new Error(res.message || '');
                        const pid = res.playlist.id;
                        let chain = Promise.resolve();
                        snap.queue.forEach(function (entry) {
                            chain = chain.then(function () {
                                return postJSON('/api/playlists/' + pid + '/add',
                                                { conversion_id: entry.id });
                            });
                        });
                        return chain.then(function () {
                            if (nameInput) nameInput.value = '';
                            if (typeof toast === 'function') {
                                toast('Queue saved as playlist.', 'success');
                            }
                            loadUserPlaylists();
                        });
                    })
                    .catch(function () {
                        if (typeof toast === 'function') toast('Could not save queue.', 'danger');
                    });
            });
        }
        const form = document.getElementById('tabPlaylistCreate');
        if (form) {
            form.addEventListener('submit', function (e) {
                e.preventDefault();
                const input = document.getElementById('tabPlaylistName');
                const name = input.value.trim();
                if (!name) return;
                postJSON('/api/playlists', { name: name })
                    .then(function (res) {
                        if (!res.ok) {
                            if (typeof toast === 'function') toast(res.message || 'Could not create.', 'danger');
                            return;
                        }
                        input.value = '';
                        loadUserPlaylists();
                    })
                    .catch(function () {
                        if (typeof toast === 'function') toast('Could not create.', 'danger');
                    });
            });
        }
        document.querySelectorAll('[data-ptab]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                if (!window.AudioPlayer) return;
                const action = btn.dataset.ptab;
                if (action === 'toggle') window.AudioPlayer.toggle();
                else if (action === 'next') window.AudioPlayer.next();
                else if (action === 'prev') window.AudioPlayer.prev();
            });
        });
        setInterval(syncNowPlaying, 1000);
        scheduleViz();
    }

    document.addEventListener('click', function (e) {
            const rate = e.target.closest ? e.target.closest('[data-rate]') : null;
            if (rate) {
                const parts = String(rate.dataset.rate).split(':');
                postJSON('/api/rate/' + parts[0], { rating: Number(parts[1]) })
                    .then(function (res) {
                        if (!res.ok) return;
                        const item = library.find(function (it) { return String(it.id) === String(parts[0]); });
                        if (item) item.rating = res.rating;
                        renderLibrary();
                    })
                    .catch(function () {});
                e.preventDefault();
                return;
            }
            const like = e.target.closest ? e.target.closest('[data-like]') : null;
            if (!like) return;
            postJSON('/api/like/' + like.dataset.like, {})
                .then(function (res) {
                    if (!res.ok) return;
                    const item = library.find(function (it) { return String(it.id) === String(like.dataset.like); });
                    if (item) item.liked = res.liked;
                    like.classList.toggle('liked', res.liked);
                    like.textContent = res.liked ? '♥' : '♡';
                    renderLibrary();
                })
                .catch(function () {});
            e.preventDefault();
        });

    document.addEventListener('DOMContentLoaded', init);
})();
