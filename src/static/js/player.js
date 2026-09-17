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
            speed: $('appPlayerSpeed'),
            sleep: $('appPlayerSleep'),
            eqBtn: $('appPlayerEQBtn'),
            eqBox: $('appPlayerEQBox'),
            eqReset: $('appPlayerEQReset'),
        };

        // Restore volume
        const vol = localStorage.getItem('appPlayerVolume');
        if (vol !== null) {
            const v = Math.max(0, Math.min(100, Number(vol)));
            Player.ui.volume.value = v;
            Player.audio.volume = v / 100;
        }
        restoreSpeed();
        restoreRepeat();
        restoreQueue();
        restoreEQ();

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
        Player.ui.queueClear.addEventListener('click', () => {
            Player.queue = [];
            Player.index = -1;
            saveQueue();
            hide();
            renderQueue();
        });
        Player.ui.shuffle.addEventListener('click', () => shuffleQueue());
        Player.ui.repeat.addEventListener('click', () => cycleRepeat());
        Player.ui.speed.addEventListener('change', () => {
            const rate = Number(Player.ui.speed.value) || 1;
            Player.audio.playbackRate = rate;
            try {
                localStorage.setItem('appPlayerSpeed', String(rate));
            } catch (err) { /* private mode */ }
        });
        Player.ui.sleep.addEventListener('change', () => {
            setSleepTimer(Number(Player.ui.sleep.value) || 0);
        });
        Player.ui.eqBtn.addEventListener('click', () => {
            Player.ui.eqBox.classList.toggle('d-none');
        });
        Player.ui.eqReset.addEventListener('click', () => {
            applyEQGains([0, 0, 0, 0, 0]);
        });
        Array.from(document.querySelectorAll('.app-player-eq-slider')).forEach((slider) => {
            slider.addEventListener('input', () => {
                const gains = currentEQGains();
                gains[Number(slider.dataset.eq)] = Number(slider.value);
                applyEQGains(gains);
            });
        });
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
        saveQueue();
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
            saveQueue();
            loadCurrent();
            play();
        } else {
            saveQueue();
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
                saveQueue();
                hide();
                return;
            }
            Player.index = Math.min(Player.index, Player.queue.length - 1);
            saveQueue();
            loadCurrent();
            play();
            return;
        }
        saveQueue();
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
        Player.ui.cover.classList.add('d-none');
        Player.ui.cover.removeAttribute('src');
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
                Player.ui.cover.onload = () => Player.ui.cover.classList.remove('d-none');
                Player.ui.cover.onerror = () => Player.ui.cover.classList.add('d-none');
                Player.ui.cover.src = '/api/cover/' + item.id;
                Player.audio.load();
                if (!Player.audio.paused) Player.audio.pause();
                play();
            })
            .catch(function () {
                Player.ui.title.textContent = 'Could not load track';
            });
    }

    // ------------------------------------------------- persistence ----

    function saveQueue() {
        try {
            localStorage.setItem('appPlayerQueue', JSON.stringify({
                items: Player.queue,
                index: Player.index,
                context: Player.contextLabel,
                repeat: Player.repeat,
            }));
        } catch (err) { /* private mode */ }
    }

    function restoreQueue() {
        let saved = null;
        try {
            saved = JSON.parse(localStorage.getItem('appPlayerQueue') || 'null');
        } catch (err) { saved = null; }
        if (!saved || !Array.isArray(saved.items) || !saved.items.length) return;
        Player.queue = saved.items.filter((item) => item && item.id);
        if (!Player.queue.length) return;
        Player.index = Math.max(0, Math.min(Number(saved.index) || 0, Player.queue.length - 1));
        Player.contextLabel = saved.context || '';
        if (saved.repeat) Player.repeat = saved.repeat;
        updateRepeatLabel();
        const current = Player.queue[Player.index];
        Player.ui.title.textContent = (current && current.display) || 'Not playing';
        Player.ui.context.textContent = Player.contextLabel || '';
        Player.ui.context.classList.toggle('d-none', !Player.contextLabel);
        Player.ui.player.classList.remove('d-none');
        renderQueue();
    }

    function restoreRepeat() {
        try {
            const mode = localStorage.getItem('appPlayerRepeat');
            if (mode === 'all' || mode === 'one' || mode === 'off') {
                Player.repeat = mode;
            }
        } catch (err) { /* private mode */ }
        updateRepeatLabel();
    }

    function cycleRepeat() {
        Player.repeat = Player.repeat === 'off' ? 'all'
            : Player.repeat === 'all' ? 'one' : 'off';
        try {
            localStorage.setItem('appPlayerRepeat', Player.repeat);
        } catch (err) { /* private mode */ }
        updateRepeatLabel();
        saveQueue();
    }

    function updateRepeatLabel() {
        if (!Player.ui.repeat) return;
        const label = Player.repeat === 'all' ? 'Repeat: All'
            : Player.repeat === 'one' ? 'Repeat: One' : 'Repeat: Off';
        Player.ui.repeat.textContent = label;
        Player.ui.repeat.title = 'Repeat mode: ' + Player.repeat;
    }

    function shuffleQueue() {
        if (Player.queue.length < 2) return;
        const head = Player.queue.slice(0, Player.index + 1);
        const tail = Player.queue.slice(Player.index + 1);
        for (let i = tail.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            const tmp = tail[i];
            tail[i] = tail[j];
            tail[j] = tmp;
        }
        Player.queue = head.concat(tail);
        saveQueue();
        renderQueue();
    }

    function restoreSpeed() {
        try {
            const rate = Number(localStorage.getItem('appPlayerSpeed')) || 1;
            Player.audio.playbackRate = rate;
            if (Player.ui.speed) Player.ui.speed.value = String(rate);
        } catch (err) { /* private mode */ }
    }

    let sleepTimer = null;

    function setSleepTimer(minutes) {
        if (sleepTimer) {
            clearTimeout(sleepTimer);
            sleepTimer = null;
        }
        if (minutes > 0) {
            sleepTimer = setTimeout(() => {
                Player.audio.pause();
                if (typeof toast === 'function') toast('Sleep timer stopped playback.', 'info');
                if (Player.ui.sleep) Player.ui.sleep.value = '0';
            }, minutes * 60 * 1000);
        }
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
        ensureEQ();
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
        if (Player.repeat === 'one') {
            seekTo(0);
            play();
            return;
        }
        if (Player.index < Player.queue.length - 1) {
            Player.index += 1;
            saveQueue();
            loadCurrent();
        } else if (Player.repeat === 'all' && Player.queue.length > 1) {
            Player.index = 0;
            saveQueue();
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

    // ------------------------------------------------------------------ eq

    const EQ_FREQS = [60, 230, 910, 3600, 14000];
    let eqNodes = null;

    // Routes audio through a 5-band equalizer on first play. Created lazily
    // (and only once per page) so pages that never play stay untouched.
    function ensureEQ() {
        if (eqNodes || !window.AudioContext) return;
        try {
            const ctx = new AudioContext();
            const src = ctx.createMediaElementSource(Player.audio);
            let node = src;
            const filters = EQ_FREQS.map(function (freq) {
                const filter = ctx.createBiquadFilter();
                filter.type = 'peaking';
                filter.frequency.value = freq;
                filter.Q.value = 1;
                filter.gain.value = 0;
                node.connect(filter);
                node = filter;
                return filter;
            });
            node.connect(ctx.destination);
            eqNodes = { ctx: ctx, filters: filters };
            applyEQGains(currentEQGains());
        } catch (err) { eqNodes = null; }
    }

    function currentEQGains() {
        const gains = [];
        Array.from(document.querySelectorAll('.app-player-eq-slider')).forEach(function (slider) {
            gains[Number(slider.dataset.eq)] = Number(slider.value);
        });
        while (gains.length < EQ_FREQS.length) gains.push(0);
        return gains.slice(0, EQ_FREQS.length);
    }

    function applyEQGains(gains) {
        Array.from(document.querySelectorAll('.app-player-eq-slider')).forEach(function (slider) {
            slider.value = String(gains[Number(slider.dataset.eq)] || 0);
        });
        if (eqNodes) {
            eqNodes.filters.forEach(function (filter, i) {
                filter.gain.value = gains[i] || 0;
            });
        }
        try {
            localStorage.setItem('appPlayerEQ', JSON.stringify(gains));
        } catch (err) { /* private mode */ }
    }

    function restoreEQ() {
        let gains = null;
        try {
            gains = JSON.parse(localStorage.getItem('appPlayerEQ') || 'null');
        } catch (err) { gains = null; }
        if (Array.isArray(gains) && gains.length) {
            applyEQGains(gains);
        }
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