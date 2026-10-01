// Player tab: library grid, now-playing mirror, user playlists, visualizer.
// Drives the shared engine in player.js through window.AudioPlayer; the
// bottom bar stays the single owner of playback state.
(function () {
    'use strict';

    var library = [];
    var userPlaylists = [];
    var lastCoverId = null;
    var lastSnapshotSig = '';

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function apiHeaders() {
        var token = '';
        try {
            var meta = document.querySelector('meta[name="csrf-token"]');
            token = meta ? meta.getAttribute('content') : '';
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
            if (q && (trackTitle(item) + ' ' + (item.output_path || '')).toLowerCase().indexOf(q) === -1) {
                return false;
            }
            return true;
        });
        if (sort === 'played') {
            rows.sort(function (a, b) { return (b.play_count || 0) - (a.play_count || 0); });
        } else if (sort === 'name') {
            rows.sort(function (a, b) {
                return trackTitle(a).localeCompare(trackTitle(b));
            });
        }
        grid.innerHTML = '';
        if (!rows.length) {
            grid.innerHTML = '<span class="text-muted small">Nothing here yet — convert something first.</span>';
            return;
        }
        rows.forEach(function (item) {
            var card = document.createElement('div');
            card.className = 'lib-card';
            var img = document.createElement('img');
            img.className = 'lib-cover';
            img.alt = '';
            img.loading = 'lazy';
            img.src = '/api/cover/' + item.id;
            img.onerror = function () { img.classList.add('d-none'); };
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

    // ------------------------------------------------------------------ init

    function init() {
        if (!document.getElementById('libGrid')) return;
        loadUserPlaylists();
        loadLibrary();
        const search = document.getElementById('tabSearch');
        if (search) search.addEventListener('input', renderLibrary);
        const liked = document.getElementById('tabLikedOnly');
        if (liked) liked.addEventListener('change', renderLibrary);
        const sort = document.getElementById('tabSort');
        if (sort) sort.addEventListener('change', renderLibrary);
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
