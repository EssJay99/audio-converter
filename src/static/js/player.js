// In-app media player: play/pause, prev/next, seek, volume, and a buildable
// queue. Tracks are streamed from /audio/<id> (Range-enabled) and title/artist
// metadata comes from /api/track/<id> (ffprobe).

(function () {
    'use strict';

    const Player = {
        queue: [],
        index: -1,
        audio: null,
        contextLabel: '',
        repeat: 'off',
        ui: {},
    };

    function $(id) { return document.getElementById(id); }

    function init() {
        Player.audio = $('appPlayerAudio');
        if (!Player.audio) return; // player markup not present on this page

        Player.ui = {
            player: $('appPlayer'),
            play: $('appPlayerPlay'),
            prev: $('appPlayerPrev'),
            next: $('appPlayerNext'),
            seek: $('appPlayerSeek'),
            time: $('appPlayerTime'),
            dur: $('appPlayerDur'),
            title: $('appPlayerTitle'),
            artist: $('appPlayerArtist'),
            tag: $('appPlayerTag'),
            context: $('appPlayerContext'),
            volume: $('appPlayerVolume'),
            queueBtn: $('appPlayerQueueBtn'),
            queueCount: $('appPlayerQueueCount'),
            queueBox: $('appPlayerQueueBox'),
            queueList: $('appPlayerQueueList'),
            queueClear: $('appPlayerQueueClear'),
            close: $('appPlayerClose'),
            shuffle: $('appPlayerShuffle'),
            repeat: $('appPlayerRepeat'),
            cover: $('appPlayerCover'),
        };

        // Restore volume
        const vol = localStorage.getItem('appPlayerVolume');
        if (vol !== null) {
            const v = Math.max(0, Math.min(100, Number(vol)));
            Player.ui.volume.value = v;
            Player.audio.volume = v / 100;
        }
        restoreRepeat();
        restoreQueue();

        Player.audio.addEventListener('timeupdate', onTimeUpdate);
        Player.audio.addEventListener('loadedmetadata', onLoadedMetadata);
        Player.audio.addEventListener('play', () => {
            Player.ui.play.textContent = '\u2759\u2759';
            Player.ui.play.setAttribute('aria-label', 'Pause');
        });
        Player.audio.addEventListener('pause', () => {
            Player.ui.play.textContent = '\u25B6';
            Player.ui.play.setAttribute('aria-label', 'Play');
        });
        Player.audio.addEventListener('ended', () => next());
        Player.audio.addEventListener('error', () => {
            Player.ui.title.textContent = 'Cannot play this format in the browser';
            Player.ui.artist.textContent = '';
        });

        Player.ui.play.addEventListener('click', togglePlay);
        Player.ui.prev.addEventListener('click', () => prev());
        Player.ui.next.addEventListener('click', () => next());
        Player.ui.close.addEventListener('click', () => hide());
        Player.ui.seek.addEventListener('input', () => seekTo(Player.ui.seek.value / 1000));
        Player.ui.volume.addEventListener('input', () => {
            const v = Number(Player.ui.volume.value);
            Player.audio.volume = v / 100;
            localStorage.setItem('appPlayerVolume', String(v));
        });
        Player.ui.queueBtn.addEventListener('click', () => toggleQueueBox());
        Player.ui.queueClear.addEventListener('click', () => { Player.queue = []; renderQueue(); });
        Player.ui.queueList.addEventListener('click', (e) => {
            const rem = e.target.closest('[data-queue-remove]');
            if (rem) {
                removeFromQueue(Number(rem.dataset.queueRemove));
                e.preventDefault();
                return;
            }
            const jump = e.target.closest('[data-queue-index]');
            if (jump) {
                const idx = Number(jump.dataset.queueIndex);
                if (idx >= 0 && idx < Player.queue.length && idx !== Player.index) {
                    Player.index = idx;
                    loadCurrent();
                    play();
                }
                e.preventDefault();
            }
        });
        document.addEventListener('click', onRowButtonClick);
    }

    // ------------------------------------------------------------------ api

    function fmtTime(seconds) {
        if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return m + ':' + String(s).padStart(2, '0');
    }

    function stem(name) {
        return String(name || '').replace(/\.[^.]+$/, '');
    }

    // ----------------------------------------------------------------- queue

    function buildLinearQueue(items, startIndex, contextLabel) {
        // items: raw serialized conversions (already completed & on disk).
        Player.queue = items.map(function (item) {
            return {
                id: item.id,
                display: item.filename ? stem(item.filename) : (item.playlist_title || 'Track'),
                format: item.format,
            };
        });
        Player.index = Math.max(0, Math.min(startIndex, Player.queue.length - 1));
        Player.contextLabel = contextLabel || '';
        renderQueue();
        loadCurrent();
        play();
    }

    function enqueue(item) {
        Player.queue.push({
            id: item.id,
            display: item.filename ? stem(item.filename) : 'Track',
            format: item.format,
        });
        if (Player.index < 0) {
            Player.index = 0;
            loadCurrent();
            play();
        } else {
            renderQueue();
        }
    }

    function removeFromQueue(qidx) {
        if (qidx < 0 || qidx >= Player.queue.length) return;
        Player.queue.splice(qidx, 1);
        if (qidx < Player.index) Player.index -= 1;
        else if (qidx === Player.index) {
            if (Player.queue.length === 0) {
                Player.index = -1;
                hide();
                return;
            }
            Player.index = Math.min(Player.index, Player.queue.length - 1);
            loadCurrent();
            play();
        }
        renderQueue();
    }

    function renderQueue() {
        Player.ui.queueCount.textContent = String(Player.queue.length || 0);
        Player.ui.queueList.innerHTML = '';
        Player.queue.forEach(function (item, i) {
            const li = document.createElement('li');
            li.className = 'app-player-queue-item' + (i === Player.index ? ' active' : '');
            li.setAttribute('data-queue-index', String(i));
            const title = document.createElement('span');
            title.className = 'app-player-queue-title';
            title.textContent = (i + 1) + '. ' + item.display;
            li.appendChild(title);
            const badge = document.createElement('span');
            badge.className = 'app-play-format';
            badge.textContent = item.format;
            li.appendChild(badge);
            const rem = document.createElement('button');
            rem.type = 'button';
            rem.className = 'app-player-queue-remove';
            rem.setAttribute('data-queue-remove', String(i));
            rem.textContent = '\u00d7';
            li.appendChild(rem);
            Player.ui.queueList.appendChild(li);
        });
    }

    // ----------------------------------------------------------------- load

    function loadCurrent() {
        const item = Player.queue[Player.index];
        if (!item) return;
        fetch('/api/track/' + item.id)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.ok) throw new Error(data.message || 'unavailable');
                Player.ui.title.textContent = data.meta && data.meta.title ? data.meta.title : item.display;
                Player.ui.artist.textContent = (data.meta && data.meta.artist) || '';
                Player.ui.artist.classList.toggle('d-none', !Player.ui.artist.textContent);
                const tagBits = [];
                tagBits.push(item.format);
                if (data.item && data.item.file_exists) tagBits.push(data.item.output_path);
                Player.ui.tag.textContent = tagBits.join(' \u00b7 ');
                Player.ui.tag.classList.toggle('d-none', !Player.ui.tag.textContent);
                Player.ui.tag.title = tagBits.join(' \u00b7 ');
                Player.ui.context.textContent = Player.contextLabel || '';
                Player.ui.context.classList.toggle('d-none', !Player.contextLabel);
                show();
                Player.audio.src = '/audio/' + item.id;
                Player.audio.load();
                if (!Player.audio.paused) Player.audio.pause();
                play();
            })
            .catch(function () {
                Player.ui.title.textContent = 'Could not load track';
            });
    }

    // -------------------------------------------------------------- controls

    function show() {
        Player.ui.player.classList.remove('d-none');
        Player.ui.artist.classList.remove('d-none');
        renderQueue();
    }

    function hide() {
        Player.audio.pause();
        Player.ui.player.classList.add('d-none');
        toggleQueueBox(true);
    }

    function play() {
        if (Player.audio.src && Player.audio.src !== location.href) {
            Player.audio.play().catch(function () {});
        }
    }

    function togglePlay() {
        if (Player.audio.paused) {
            play();
        } else {
            Player.audio.pause();
        }
    }

    function prev() {
        if (Player.queue.length === 0) return;
        if (Player.audio.currentTime > 3) {
            seekTo(0);
            return;
        }
        Player.index = Player.index > 0 ? Player.index - 1 : 0;
        loadCurrent();
    }

    function next() {
        if (Player.queue.length === 0) return;
        if (Player.index < Player.queue.length - 1) {
            Player.index += 1;
            loadCurrent();
        } else {
            Player.audio.pause();
            Player.audio.currentTime = 0;
        }
    }

    function seekTo(ratio) {
        if (!Player.audio.duration || !Number.isFinite(Player.audio.duration)) return;
        Player.audio.currentTime = ratio * Player.audio.duration;
    }

    function onTimeUpdate() {
        if (!Player.audio.duration || !Number.isFinite(Player.audio.duration)) return;
        const ratio = (Player.audio.currentTime / Player.audio.duration) * 1000;
        Player.ui.seek.value = String(Math.round(ratio));
        Player.ui.time.textContent = fmtTime(Player.audio.currentTime);
    }

    function onLoadedMetadata() {
        Player.ui.dur.textContent = fmtTime(Player.audio.duration);
    }

    function toggleQueueBox(forceHidden) {
        if (typeof forceHidden === 'boolean') {
            Player.ui.queueBox.classList.toggle('d-none', forceHidden);
            return;
        }
        Player.ui.queueBox.classList.toggle('d-none');
    }

    // --------------------------------------------------------------- buttons

    // Row buttons are delegated so dynamically-rendered rows (poll refresh,
    // lazy playlist children) work without re-binding:
    //   [data-play]       play this track / start its playlist from here
    //   [data-play-all]   play every completed track of a playlist
    //   [data-queue]      append this track to the current queue
    function onRowButtonClick(e) {
        const queueBtn = e.target.closest('[data-queue]');
        if (queueBtn) {
            fetch('/api/track/' + queueBtn.dataset.queue)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!data.ok) throw new Error(data.message || '');
                    enqueue(data.item);
                })
                .catch(function () { alert('Could not add this track to the queue.'); });
            e.preventDefault();
            return;
        }

        const playAllBtn = e.target.closest('[data-play-all]');
        if (playAllBtn) {
            fetch('/api/playlist/' + playAllBtn.dataset.playAll)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    const items = (data.items || []).filter(function (it) {
                        return it.status === 'completed' && it.file_exists;
                    });
                    if (!items.length) {
                        alert('This playlist has no playable tracks yet.');
                        return;
                    }
                    buildLinearQueue(items, 0, data.items[0] && data.items[0].playlist_title ? data.items[0].playlist_title : 'Playlist');
                })
                .catch(function () { alert('Could not load this playlist.'); });
            e.preventDefault();
            return;
        }

        const playBtn = e.target.closest('[data-play]');
        if (playBtn) {
            const id = playBtn.dataset.play;
            const row = playBtn.closest('tr');
            const parentId = row ? row.dataset.playlistId : '';
            if (parentId) {
                // Playing a track inside an expanded playlist: queue the whole
                // playlist and start from this track.
                fetch('/api/playlist/' + parentId)
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        const items = (data.items || []).filter(function (it) {
                            return it.status === 'completed' && it.file_exists;
                        });
                        const start = Math.max(0, items.findIndex(function (it) { return String(it.id) === String(id); }));
                        buildLinearQueue(items, start, (items[0] && items[0].playlist_title) || 'Playlist');
                    })
                    .catch(function () { alert('Could not load this playlist.'); });
            } else {
                fetch('/api/track/' + id)
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (!data.ok) throw new Error(data.message || '');
                        buildLinearQueue([data.item], 0, '');
                    })
                    .catch(function () { alert('Could not play this track.'); });
            }
            e.preventDefault();
        }
    }

    document.addEventListener('DOMContentLoaded', init);

    // Keyboard shortcuts: Space toggles, arrows seek/volume. Ignored while
    // typing in a field so search boxes and forms keep working.
    document.addEventListener('keydown', (e) => {
        if (!Player.audio || Player.ui.player.classList.contains('d-none')) return;
        const target = e.target || {};
        const tag = target.tagName || '';
        if (/INPUT|TEXTAREA|SELECT/.test(tag) || target.isContentEditable) return;
        if (e.code === 'Space') {
            e.preventDefault();
            togglePlay();
        } else if (e.key === 'ArrowRight') {
            e.preventDefault();
            seekBy(5);
        } else if (e.key === 'ArrowLeft') {
            e.preventDefault();
            seekBy(-5);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            nudgeVolume(5);
        } else if (e.key === 'ArrowDown') {
            e.preventDefault();
            nudgeVolume(-5);
        }
    });

    function seekBy(seconds) {
        if (!Player.audio.duration || !Number.isFinite(Player.audio.duration)) return;
        Player.audio.currentTime = Math.max(
            0, Math.min(Player.audio.duration, Player.audio.currentTime + seconds));
    }

    function nudgeVolume(delta) {
        const v = Math.max(0, Math.min(100, Number(Player.ui.volume.value) + delta));
        Player.ui.volume.value = v;
        Player.audio.volume = v / 100;
        localStorage.setItem('appPlayerVolume', String(v));
    }
})();