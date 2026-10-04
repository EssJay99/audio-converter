// In-app media player: play/pause, prev/next, seek, volume, and a buildable
// queue. Tracks are streamed from /audio/<id> (Range-enabled) and title/artist
// metadata comes from /api/track/<id> (ffprobe).

(function () {
    'use strict';

    const Player = {
        queue: [],
        index: -1,
        audio: null,
        audio2: null,
        eqWired2: false,
        fading: false,
        fadeTimer: null,
        fadeState: null,
        crossfade: 0,
        sleepMode: null,
        expectResume: 0,
        lastPosSave: 0,
        contextLabel: '',
        repeat: 'off',
        ui: {},
    };

    function $(id) { return document.getElementById(id); }

    function init() {
        Player.audio = $('appPlayerAudio');
        if (!Player.audio) return; // player markup not present on this page
        // Second element for crossfades: the EQ graph taps both, the UI
        // stays bound to Player.audio, and the new track hands off to it.
        Player.audio2 = new Audio();
        Player.audio2.preload = 'auto';

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
            if (Player.audio2) Player.audio2.volume = v / 100;
        }
        restoreSpeed();
        restoreRepeat();
        restoreQueue();
        restoreEQ();
        restorePreampBalance();
        restoreFadeSecs();
        restoreAutoLyrics();
        restoreCrossfade();
        restoreSleepMode();
        initMediaSession();

        Player.audio.addEventListener('timeupdate', onTimeUpdate);
        Player.audio.addEventListener('loadedmetadata', onLoadedMetadata);
        Player.audio.addEventListener('play', () => {
            Player.ui.play.textContent = '❙❙';
            Player.ui.play.setAttribute('aria-label', 'Pause');
            updatePositionState();
        });
        Player.audio.addEventListener('pause', () => {
            Player.ui.play.textContent = '▶';
            Player.ui.play.setAttribute('aria-label', 'Play');
            savePosition();
        });
        Player.audio.addEventListener('ended', () => {
            clearPosition();
            next(true);
        });
        const theaterVideo = $('appTheaterVideo');
        if (theaterVideo) {
            theaterVideo.addEventListener('ended', () => {
                clearPosition();
                if (!showUpNext()) next(true);
            });
            theaterVideo.addEventListener('loadedmetadata', onTheaterLoaded);
            theaterVideo.addEventListener('timeupdate', onTheaterTime);
            theaterVideo.addEventListener('pause', () => saveTheaterPosition());
            theaterVideo.addEventListener('error', () => {
                const dl = $('appTheaterDownload');
                if (typeof toast === 'function') {
                    toast('This video format will not play here — use Download.', 'info');
                }
                if (dl) dl.focus();
            });
        }
        const theaterClose = $('appTheaterClose');
        if (theaterClose) {
            theaterClose.addEventListener('click', () => closeTheater(false));
        }
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
            if (Player.audio2) Player.audio2.volume = v / 100;
            localStorage.setItem('appPlayerVolume', String(v));
            updateMuteIcon();
        });
        const muteBtn = $('appPlayerMute');
        if (muteBtn) {
            muteBtn.addEventListener('click', () => toggleMute());
            updateMuteIcon();
        }
        Player.ui.queueBtn.addEventListener('click', () => toggleQueueBox());
        initQueueDrag();
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
            const video = $('appTheaterVideo');
            if (video) video.playbackRate = rate;
            try {
                localStorage.setItem('appPlayerSpeed', String(rate));
            } catch (err) { /* private mode */ }
        });
        Player.ui.sleep.addEventListener('change', () => {
            setSleepTimer(Player.ui.sleep.value);
        });
        const crossfadeSel = $('appPlayerCrossfade');
        if (crossfadeSel) {
            crossfadeSel.addEventListener('change', () => {
                Player.crossfade = Number(crossfadeSel.value) || 0;
                try {
                    localStorage.setItem('appPlayerCrossfade', String(Player.crossfade));
                } catch (err) { /* private mode */ }
            });
        }
        const shotBtn = $('appTheaterShot');
        if (shotBtn) shotBtn.addEventListener('click', snapshotFrame);
        const fullBtn = $('appTheaterFull');
        if (fullBtn) {
            fullBtn.addEventListener('click', () => {
                const overlay = $('appTheater');
                if (!overlay) return;
                try {
                    if (document.fullscreenElement) {
                        document.exitFullscreen().catch(function () {});
                    } else if (overlay.requestFullscreen) {
                        overlay.requestFullscreen().catch(function () {});
                    }
                } catch (err) { /* ignore */ }
            });
        }
        const upPlay = $('appUpNextPlay');
        if (upPlay) {
            upPlay.addEventListener('click', () => {
                hideUpNext();
                next(true);
            });
        }
        const upDismiss = $('appUpNextDismiss');
        if (upDismiss) {
            upDismiss.addEventListener('click', () => hideUpNext());
        }
        const picBtn = $('appTheaterPicBtn');
        if (picBtn) {
            picBtn.addEventListener('click', () => {
                const box = $('appTheaterPicBox');
                if (box) box.classList.toggle('d-none');
            });
        }
        const picReset = $('appPicReset');
        if (picReset) {
            picReset.addEventListener('click', () => {
                Object.assign(theaterPic, { b: 100, c: 100, s: 100, fit: 'contain', sub: '1em' });
                syncPicControls();
                applyTheaterPic();
            });
        }
        ['appPicBright', 'appPicContrast', 'appPicSaturate'].forEach((id, i) => {
            const slider = $(id);
            if (slider) {
                slider.addEventListener('input', () => {
                    theaterPic[['b', 'c', 's'][i]] = Number(slider.value) || 100;
                    applyTheaterPic();
                });
            }
        });
        const fitSel = $('appTheaterFit');
        if (fitSel) {
            fitSel.addEventListener('change', () => {
                theaterPic.fit = fitSel.value || 'contain';
                applyTheaterPic();
            });
        }
        const subSel = $('appTheaterSubSize');
        if (subSel) {
            subSel.addEventListener('change', () => {
                theaterPic.sub = subSel.value || '1em';
                applyTheaterPic();
            });
        }
        restoreTheaterPic();
        const pipBtn = $('appTheaterPip');
        if (pipBtn) {
            if (!document.pictureInPictureEnabled) pipBtn.classList.add('d-none');
            pipBtn.addEventListener('click', () => {
                const video = $('appTheaterVideo');
                if (!video) return;
                try {
                    const p = video.requestPictureInPicture();
                    if (p && p.catch) {
                        p.catch(function () {
                            if (typeof toast === 'function') toast('Picture in picture is not available for this video.', 'info');
                        });
                    }
                } catch (err) {
                    if (typeof toast === 'function') toast('Picture in picture is not available for this video.', 'info');
                }
            });
        }
        const qExport = $('appPlayerQueueExport');
        if (qExport) qExport.addEventListener('click', exportQueue);
        const qClearPlayed = $('appPlayerQueueClearPlayed');
        if (qClearPlayed) {
            qClearPlayed.addEventListener('click', () => {
                clearPlayed();
                if (typeof toast === 'function') toast('Played tracks cleared.', 'info');
            });
        }
        const qSearch = $('appPlayerQueueSearch');
        if (qSearch) qSearch.addEventListener('input', () => renderQueue());
        const fadeSel = $('appPlayerFadeSecs');
        if (fadeSel) {
            fadeSel.addEventListener('change', () => {
                Player.fadeSecs = Math.max(2, Math.min(30, Number(fadeSel.value) || 8));
                try {
                    localStorage.setItem('appPlayerFadeSecs', String(Player.fadeSecs));
                } catch (err) { /* private mode */ }
            });
        }
        const autoLy = $('appAutoLyrics');
        if (autoLy) {
            autoLy.addEventListener('change', () => {
                Player.autoLyrics = autoLy.checked;
                try {
                    localStorage.setItem('appPlayerAutoLyrics', autoLy.checked ? '1' : '0');
                } catch (err) { /* private mode */ }
            });
        }
        const qImport = $('appPlayerQueueImport');
        if (qImport) {
            qImport.addEventListener('change', () => {
                if (qImport.files && qImport.files.length) importQueueFile(qImport.files[0]);
                qImport.value = '';
            });
        }
        const lyricsBtn = $('appPlayerLyricsBtn');
        if (lyricsBtn) {
            lyricsBtn.addEventListener('click', () => {
                const id = currentLyricId();
                if (id) Lyrics.open(id);
                else if (typeof toast === 'function') toast('Nothing playing.', 'info');
            });
        }
        const theaterLyrics = $('appTheaterLyrics');
        if (theaterLyrics) {
            theaterLyrics.addEventListener('click', () => {
                if (theaterTrackId()) Lyrics.open(theaterTrackId());
            });
        }
        const lyricsClose = $('appLyricsClose');
        if (lyricsClose) lyricsClose.addEventListener('click', () => Lyrics.close());
        const bell = $('appBellBtn');
        if (bell) bell.addEventListener('click', () => toggleNotices());
        const noticesClear = $('appNoticesClear');
        if (noticesClear) {
            noticesClear.addEventListener('click', () => {
                postNotice('read', true).then(loadNotices);
            });
        }
        const scClose = $('appShortcutsClose');
        if (scClose) {
            scClose.addEventListener('click', () => {
                const sc = $('appShortcuts');
                if (sc) sc.classList.add('d-none');
            });
        }
        setInterval(refreshBell, 60000);
        refreshBell();
        Player.ui.eqBtn.addEventListener('click', () => {
            Player.ui.eqBox.classList.toggle('d-none');
        });
        Player.ui.eqReset.addEventListener('click', () => {
            applyEQGains([0, 0, 0, 0, 0]);
        });
        Array.from(document.querySelectorAll('[data-eq-preset]')).forEach((btn) => {
            btn.addEventListener('click', () => {
                const gains = EQ_PRESETS[btn.dataset.eqPreset];
                if (gains) applyEQGains(gains.slice());
            });
        });
        Array.from(document.querySelectorAll('.app-player-eq-slider')).forEach((slider) => {
            slider.addEventListener('input', () => {
                const gains = currentEQGains();
                gains[Number(slider.dataset.eq)] = Number(slider.value);
                applyEQGains(gains);
            });
        });
        const preEl = $('appPlayerPreamp');
        if (preEl) {
            preEl.addEventListener('input', () => {
                try {
                    localStorage.setItem('appPlayerPreamp', preEl.value);
                } catch (err) { /* private mode */ }
                applyPreampBalance();
            });
        }
        const balEl = $('appPlayerBalance');
        if (balEl) {
            balEl.addEventListener('input', () => {
                try {
                    localStorage.setItem('appPlayerBalance', balEl.value);
                } catch (err) { /* private mode */ }
                applyPreampBalance();
            });
        }
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
                path: item.output_path || '',
            };
        });
        Player.index = Math.max(0, Math.min(startIndex, Player.queue.length - 1));
        Player.contextLabel = contextLabel || '';
        saveQueue();
        renderQueue();
        loadCurrent();
        play();
    }

    function enqueue(item, prefetched) {
        Player.queue.push({
            id: item.id,
            display: item.filename ? stem(item.filename) : 'Track',
            format: item.format,
            path: item.output_path || '',
        });
        if (Player.index < 0) {
            Player.index = 0;
            saveQueue();
            loadCurrent(prefetched);
            play();
        } else {
            saveQueue();
            renderQueue();
        }
    }

    function playNext(item) {
        const entry = {
            id: item.id,
            display: item.filename ? stem(item.filename) : 'Track',
            format: item.format,
            path: item.output_path || '',
        };
        if (Player.index < 0 || !Player.queue.length) {
            enqueue(item);
            return;
        }
        Player.queue.splice(Player.index + 1, 0, entry);
        saveQueue();
        renderQueue();
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

    function queueFilter() {
        const el = $('appPlayerQueueSearch');
        return el ? el.value.trim().toLowerCase() : '';
    }

    function clearPlayed() {
        if (Player.index <= 0) return;
        Player.queue.splice(0, Player.index);
        Player.index = 0;
        saveQueue();
        renderQueue();
    }

    function renderQueue() {
        Player.ui.queueCount.textContent = String(Player.queue.length || 0);
        Player.ui.queueList.innerHTML = '';
        const q = queueFilter();
        Player.queue.forEach(function (item, i) {
            if (q && (item.display || '').toLowerCase().indexOf(q) === -1) return;
            const li = document.createElement('li');
            li.className = 'app-player-queue-item' + (i === Player.index ? ' active' : '');
            li.setAttribute('data-queue-index', String(i));
            li.draggable = true;
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
            rem.textContent = '×';
            li.appendChild(rem);
            Player.ui.queueList.appendChild(li);
        });
    }

    let dragIndex = null;

    function initQueueDrag() {
        const list = Player.ui.queueList;
        if (!list || list.dataset.dragInit) return;
        list.dataset.dragInit = '1';
        list.addEventListener('dragstart', (e) => {
            const li = e.target.closest ? e.target.closest('[data-queue-index]') : null;
            if (!li || e.target.closest('[data-queue-remove]')) {
                e.preventDefault();
                return;
            }
            dragIndex = Number(li.dataset.queueIndex);
            try {
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', String(dragIndex));
            } catch (err) { /* ignore */ }
            li.classList.add('dragging');
        });
        list.addEventListener('dragend', () => {
            dragIndex = null;
            Array.from(list.children).forEach((child) => {
                child.classList.remove('dragging');
                child.classList.remove('drag-over');
            });
        });
        list.addEventListener('dragover', (e) => {
            if (dragIndex === null) return;
            e.preventDefault();
            const li = e.target.closest ? e.target.closest('[data-queue-index]') : null;
            Array.from(list.children).forEach((child) => child.classList.remove('drag-over'));
            if (li && Number(li.dataset.queueIndex) !== dragIndex) {
                li.classList.add('drag-over');
            }
        });
        list.addEventListener('drop', (e) => {
            if (dragIndex === null) return;
            e.preventDefault();
            const li = e.target.closest ? e.target.closest('[data-queue-index]') : null;
            moveQueueItem(dragIndex, li ? Number(li.dataset.queueIndex) : Player.queue.length - 1);
            dragIndex = null;
        });
    }

    function moveQueueItem(from, to) {
        if (from < 0 || from >= Player.queue.length) return;
        to = Math.max(0, Math.min(Player.queue.length - 1, to));
        if (from === to) {
            renderQueue();
            return;
        }
        const currentId = Player.queue[Player.index] ? Player.queue[Player.index].id : null;
        const moved = Player.queue.splice(from, 1)[0];
        Player.queue.splice(to, 0, moved);
        Player.index = Player.queue.findIndex((item) => item.id === currentId);
        saveQueue();
        renderQueue();
    }

    // ----------------------------------------------------------------- load

    function isVideoFormat(format) {
        return /video$/i.test(format || '');
    }

    function loadCurrent(prefetched) {
        const item = Player.queue[Player.index];
        if (!item) return;
        fetchAndLoad(item, false, prefetched);
    }

    // Fetch a track's metadata, then either load it straight onto the
    // primary element or crossfade into it when this is an automatic
    // advance with overlap armed.
    function fetchAndLoad(item, auto, prefetched) {
        if (Player.fading) finishFadeNow();
        const wantIndex = Player.index;
        Player.ui.cover.classList.add('d-none');
        Player.ui.cover.removeAttribute('src');
        const ready = prefetched && prefetched.item && String(prefetched.item.id) === String(item.id)
            ? Promise.resolve(prefetched)
            : fetch('/api/track/' + item.id).then(function (r) { return r.json(); });
        ready
            .then(function (data) {
                Player.autoPending = false;
                // A newer navigation (natural end during a crossfade
                // fetch, double-tap on next) already moved on: drop this
                // stale response instead of double-advancing.
                if (Player.index !== wantIndex) return;
                if (!data.ok) throw new Error(data.message || 'unavailable');
                recordPlayed(item.id);
                updateMediaSession(item, data);
                if (data.item && isVideoFormat(data.item.format)) {
                    openTheater(item, data);
                    return;
                }
                closeTheater(true);
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
                Player.expectResume = item.id;
                if (data.item && !isVideoFormat(data.item.format) && Player.autoLyrics) {
                    Lyrics.open(item.id);
                }
                if (auto && startCrossfade(item)) return;
                Player.audio.src = '/audio/' + item.id;
                Player.ui.cover.style.cursor = 'zoom-in';
        Player.ui.cover.title = 'Click to enlarge';
        Player.ui.cover.onclick = () => {
            const full = Player.ui.cover.src.replace('?size=thumb', '');
            if (window.AudioLightbox) window.AudioLightbox.open(full);
        };
        Player.ui.cover.onload = () => Player.ui.cover.classList.remove('d-none');
                Player.ui.cover.onerror = () => Player.ui.cover.classList.add('d-none');
                Player.ui.cover.src = '/api/cover/' + item.id + '?size=thumb';
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

    function restoreAutoLyrics() {
        try {
            Player.autoLyrics = localStorage.getItem('appPlayerAutoLyrics') === '1';
        } catch (err) {
            Player.autoLyrics = false;
        }
        const box = $('appAutoLyrics');
        if (box) box.checked = !!Player.autoLyrics;
    }

    function restoreFadeSecs() {
        try {
            const secs = Number(localStorage.getItem('appPlayerFadeSecs')) || 8;
            Player.fadeSecs = Math.max(2, Math.min(30, secs));
        } catch (err) {
            Player.fadeSecs = 8;
        }
        const sel = $('appPlayerFadeSecs');
        if (sel) sel.value = String(Player.fadeSecs);
    }

    // Fade the volume out instead of stopping cold.
    function fadeOutAndPause() {
        const finish = () => {
            Player.audio.pause();
            const video = $('appTheaterVideo');
            if (video) {
                try { video.pause(); } catch (err) { /* ignore */ }
            }
            if (typeof toast === 'function') toast('Sleep timer stopped playback.', 'info');
        };
        if (!eqNodes || !eqNodes.ctx || !eqNodes.master) {
            finish();
            return;
        }
        try {
            const secs = Player.fadeSecs || 8;
            const ctx = eqNodes.ctx;
            const master = eqNodes.master;
            master.gain.cancelScheduledValues(ctx.currentTime);
            master.gain.setValueAtTime(Math.max(master.gain.value, 0.0001), ctx.currentTime);
            master.gain.linearRampToValueAtTime(0.0001, ctx.currentTime + secs);
            setTimeout(() => {
                finish();
                master.gain.setValueAtTime(1, ctx.currentTime);
            }, secs * 1000 + 200);
        } catch (err) {
            finish();
        }
    }

    // ------------------------------------------------- theater ----

    // Theater mode plays video items in an overlay instead of the audio
    // bar. Formats the browser cannot decode fail on the video element and
    // fall back to a straight download link.
    function openTheater(item, data) {
        const overlay = $('appTheater');
        const video = $('appTheaterVideo');
        if (!overlay || !video) {
            if (typeof toast === 'function') {
                toast('This video needs an external player — use Download.', 'info');
            }
            return;
        }
        Player.audio.pause();
        Player.theaterId = item.id;
        hideUpNext();
        updateMediaSession(item, data);
        renderChapters(item.id);
        const title = (data.meta && data.meta.title) || item.display;
        $('appTheaterTitle').textContent = title;
        const dl = $('appTheaterDownload');
        if (dl) dl.href = '/download/' + item.id;
        const meta = $('appTheaterMeta');
        if (meta) {
            const artist = (data.meta && data.meta.artist) || '';
            meta.textContent = artist ? artist + ' · ' + item.format : item.format;
        }
        show();
        Player.ui.title.textContent = title;
        video.poster = '/api/cover/' + item.id;
        Array.from(video.querySelectorAll('track')).forEach(function (el) {
            el.remove();
        });
        fetch('/api/subs/' + item.id)
            .then(function (r) { return r.json(); })
            .then(function (subs) {
                (subs.subs || []).forEach(function (sub, i) {
                    const track = document.createElement('track');
                    track.kind = 'subtitles';
                    track.label = sub.lang;
                    track.srclang = sub.lang;
                    track.src = sub.url;
                    if (i === 0) track.default = true;
                    video.appendChild(track);
                });
            })
            .catch(function () {});
        video.dataset.itemId = String(item.id);
        resolvePlayable(item);
        try {
            video.playbackRate = Player.audio.playbackRate || 1;
        } catch (err) { /* ignore */ }
        overlay.classList.remove('d-none');
        document.body.classList.add('theater-open');
        // Playback starts in resolvePlayable once a playable URL is ready.
    }

    // Resolve a browser-playable stream: direct file, fast remux, or a
    // background transcode the theater polls until ready.
    function resolvePlayable(item) {
        const video = $('appTheaterVideo');
        if (!video) return;
        const status = $('appTheaterStatus');
        const showStatus = (text) => {
            if (!status) return;
            status.textContent = text || '';
            status.classList.toggle('d-none', !text);
        };
        fetch('/api/playable/' + item.id)
            .then(function (r) {
                if (!r.ok) throw new Error('unplayable');
                return r.json();
            })
            .then(function (data) {
                if (String(video.dataset.itemId) !== String(item.id)) return;
                if (data.state === 'ready' && data.url) {
                    showStatus('');
                    video.src = data.url;
                    const play = video.play();
                    if (play && play.catch) play.catch(function () {});
                } else if (data.state === 'working') {
                    showStatus('Preparing playable version… ' +
                        (data.progress || 0) + '%');
                    setTimeout(function () {
                        if (String(video.dataset.itemId) === String(item.id) &&
                            isTheaterOpen()) {
                            resolvePlayable(item);
                        }
                    }, 3000);
                } else {
                    throw new Error('unplayable');
                }
            })
            .catch(function () {
                if (String(video.dataset.itemId) !== String(item.id)) return;
                showStatus('');
                if (typeof toast === 'function') {
                    toast('This video format will not play here — use Download.', 'info');
                }
                const dl = $('appTheaterDownload');
                if (dl) dl.focus();
            });
    }

    let upNextTimer = null;

    function hideUpNext() {
        if (upNextTimer) {
            try { clearInterval(upNextTimer); } catch (err) {}
            upNextTimer = null;
        }
        const overlay = $('appUpNext');
        if (overlay) overlay.classList.add('d-none');
    }

    // Netflix-style card when a video ends and more is queued.
    // Returns true when it took over (caller must not advance).
    function showUpNext() {
        hideUpNext();
        if (Player.repeat === 'one') return false;
        const coming = Player.queue[Player.index + 1];
        if (!coming) return false;
        const overlay = $('appUpNext');
        if (!overlay) {
            next(true);
            return true;
        }
        $('appUpNextTitle').textContent = coming.display || 'Next video';
        $('appUpNextMeta').textContent = coming.format || '';
        const cover = $('appUpNextCover');
        if (cover) {
            cover.classList.add('d-none');
            cover.removeAttribute('src');
            cover.onload = () => cover.classList.remove('d-none');
            cover.onerror = () => cover.classList.add('d-none');
            cover.src = '/api/cover/' + coming.id + '?size=thumb';
        }
        const count = $('appUpNextCount');
        let remaining = 8;
        const tick = () => {
            remaining -= 1;
            if (count) count.textContent = String(Math.max(0, remaining));
            if (remaining <= 0) {
                hideUpNext();
                next(true);
            }
        };
        if (count) count.textContent = String(remaining);
        upNextTimer = setInterval(tick, 1000);
        overlay.classList.remove('d-none');
        return true;
    }

    function closeTheater(silent) {
        hideUpNext();
        const overlay = $('appTheater');
        const video = $('appTheaterVideo');
        if (!overlay || overlay.classList.contains('d-none')) return;
        saveTheaterPosition();
        Player.theaterId = 0;
        try { video.pause(); } catch (err) { /* ignore */ }
        video.removeAttribute('src');
        video.load();
        overlay.classList.add('d-none');
        document.body.classList.remove('theater-open');
        if (!silent && Player.audio.src) play();
    }

    function isTheaterOpen() {
        const overlay = $('appTheater');
        return !!overlay && !overlay.classList.contains('d-none');
    }

    function recordPlayed(id) {
        let token = '';
        try {
            const meta = document.querySelector('meta[name="csrf-token"]');
            token = meta ? meta.getAttribute('content') : '';
        } catch (err) { /* ignore */ }
        fetch('/api/played/' + id, {
            method: 'POST',
            headers: { 'X-CSRFToken': token },
        }).then(function () {
            document.dispatchEvent(new CustomEvent('trackplayed', { detail: { id: id } }));
        }).catch(function () {});
    }

    // -------------------------------------------------------------- controls

    function show() {
        Player.ui.player.classList.remove('d-none');
        Player.ui.artist.classList.remove('d-none');
        renderQueue();
    }

    function hide() {
        if (Player.fading) finishFadeNow();
        pauseAll();
        Player.ui.player.classList.add('d-none');
        toggleQueueBox(true);
    }

    function play() {
        if (Player.fading) finishFadeNow();
        ensureEQ();
        if (Player.audio.src && Player.audio.src !== location.href) {
            Player.audio.play().catch(function () {});
        }
    }

    function pauseAll() {
        if (Player.fadeTimer) {
            try { clearInterval(Player.fadeTimer); } catch (err) {}
            Player.fadeTimer = null;
        }
        Player.fading = false;
        Player.fadeState = null;
        try { Player.audio.pause(); } catch (err) {}
        try { Player.audio2.pause(); } catch (err) {}
        const video = $('appTheaterVideo');
        if (video) {
            try { video.pause(); } catch (err) {}
        }
    }

    function togglePlay() {
        if (Player.fading) finishFadeNow();
        if (Player.audio.paused) {
            play();
        } else {
            Player.audio.pause();
        }
    }

    function prev() {
        if (Player.fading) finishFadeNow();
        if (Player.queue.length === 0) return;
        if (Player.audio.currentTime > 3) {
            seekTo(0);
            return;
        }
        Player.index = Player.index > 0 ? Player.index - 1 : 0;
        saveQueue();
        loadCurrent();
    }

    function next(auto) {
        if (Player.fading) finishFadeNow();
        hideUpNext();
        if (Player.queue.length === 0) return;
        if (auto && Player.sleepMode === 'end-track') {
            sleepStop();
            return;
        }
        if (Player.repeat === 'one' && auto) {
            clearPosition();
            seekTo(0);
            play();
            return;
        }
        if (Player.index < Player.queue.length - 1) {
            Player.index += 1;
            saveQueue();
            fetchAndLoad(Player.queue[Player.index], !!auto);
        } else if (Player.repeat === 'all' && Player.queue.length > 1) {
            Player.index = 0;
            saveQueue();
            fetchAndLoad(Player.queue[Player.index], !!auto);
        } else if (auto && Player.sleepMode === 'end-queue') {
            sleepStop();
        } else {
            Player.audio.pause();
            Player.audio.currentTime = 0;
        }
    }

    // Automatic advance a few seconds before the end so the next track
    // overlaps instead of gap-waiting for `ended`.
    function autoAdvance() {
        if (Player.repeat === 'one') return; // let `ended` restart it
        if (Player.fading || Player.autoPending) return;
        const target = (Player.index < Player.queue.length - 1) ? Player.index + 1
            : (Player.repeat === 'all' && Player.queue.length > 1 ? 0 : -1);
        if (target < 0) return;
        Player.autoPending = true;
        Player.index = target;
        saveQueue();
        fetchAndLoad(Player.queue[Player.index], true);
    }

    function seekTo(ratio) {
        if (Player.fading) finishFadeNow();
        if (!Player.audio.duration || !Number.isFinite(Player.audio.duration)) return;
        Player.audio.currentTime = ratio * Player.audio.duration;
    }

    function onTimeUpdate() {
        if (!Player.audio.duration || !Number.isFinite(Player.audio.duration)) return;
        const ratio = (Player.audio.currentTime / Player.audio.duration) * 1000;
        Player.ui.seek.value = String(Math.round(ratio));
        Player.ui.time.textContent = fmtTime(Player.audio.currentTime);
        const remain = Player.audio.duration - Player.audio.currentTime;
        if (Player.crossfade > 0 && !Player.fading && !isTheaterOpen()
                && remain <= Player.crossfade && remain > 0.5
                && !Player.audio.paused) {
            autoAdvance();
        }
        const nowMs = Date.now();
        if (nowMs - Player.lastPosSave > 10000) {
            Player.lastPosSave = nowMs;
            savePosition();
        }
        updatePositionState();
        Lyrics.highlight(Player.audio.currentTime);
    }

    function onLoadedMetadata() {
        Player.ui.dur.textContent = fmtTime(Player.audio.duration);
        maybeResume();
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
    const EQ_PRESETS = {
        flat: [0, 0, 0, 0, 0],
        rock: [4, 3, -1, 2, 4],
        pop: [2, 4, 4, 2, 2],
        jazz: [3, 2, 0, 2, 4],
        vocal: [-2, 0, 3, 4, 2],
        bass: [6, 4, 0, -1, -2],
    };
    let eqNodes = null;

    // Routes audio through a 5-band equalizer on first play. Created lazily
    // (and only once per page) so pages that never play stay untouched.
    function ensureEQ() {
        if (!window.AudioContext) return;
        if (eqNodes) {
            wireSecondElement();
            return;
        }
        try {
            const ctx = new AudioContext();
            const src = ctx.createMediaElementSource(Player.audio);
            const preamp = ctx.createGain();
            preamp.gain.value = dbToGain(currentPreamp());
            src.connect(preamp);
            let node = preamp;
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
            const master = ctx.createGain();
            master.gain.value = 1;
            node.connect(master);
            let out = master;
            let pan = null;
            if (typeof ctx.createStereoPanner === 'function') {
                pan = ctx.createStereoPanner();
                pan.pan.value = currentBalance() / 100;
                master.connect(pan);
                out = pan;
            }
            const analyser = ctx.createAnalyser();
            analyser.fftSize = 128;
            out.connect(analyser);
            analyser.connect(ctx.destination);
            eqNodes = { ctx: ctx, filters: filters, master: master, analyser: analyser,
                        preamp: preamp, pan: pan };
            applyEQGains(currentEQGains());
            wireSecondElement();
        } catch (err) { eqNodes = null; }
    }

    // The crossfade element joins the same filter chain so EQ, volume
    // fade and visualizer treat both sources identically.
    function wireSecondElement() {
        if (!eqNodes || !Player.audio2 || Player.eqWired2) return;
        try {
            eqNodes.ctx.createMediaElementSource(Player.audio2)
                .connect(eqNodes.preamp || eqNodes.filters[0]);
            Player.eqWired2 = true;
        } catch (err) { /* already wired or unsupported */ }
    }

    function dbToGain(db) {
        return Math.pow(10, (Number(db) || 0) / 20);
    }

    function currentPreamp() {
        const el = $('appPlayerPreamp');
        return el ? Number(el.value) || 0 : 0;
    }

    function currentBalance() {
        const el = $('appPlayerBalance');
        return el ? Number(el.value) || 0 : 0;
    }

    function restorePreampBalance() {
        try {
            const pre = localStorage.getItem('appPlayerPreamp');
            const preEl = $('appPlayerPreamp');
            if (preEl && pre !== null) preEl.value = String(Math.max(-12, Math.min(12, Number(pre) || 0)));
            const bal = localStorage.getItem('appPlayerBalance');
            const balEl = $('appPlayerBalance');
            if (balEl && bal !== null) balEl.value = String(Math.max(-100, Math.min(100, Number(bal) || 0)));
        } catch (err) { /* private mode */ }
        applyPreampBalance();
    }

    function applyPreampBalance() {
        if (!eqNodes) return;
        try {
            if (eqNodes.preamp) eqNodes.preamp.gain.value = dbToGain(currentPreamp());
            if (eqNodes.pan) eqNodes.pan.pan.value = currentBalance() / 100;
        } catch (err) { /* ignore */ }
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
        const nextQueueBtn = e.target.closest('[data-queue-next]');
        if (nextQueueBtn) {
            fetch('/api/track/' + nextQueueBtn.dataset.queueNext)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!data.ok) throw new Error(data.message || '');
                    playNext(data.item);
                })
                .catch(function () {
                    if (typeof toast === 'function') toast('Could not queue this track.', 'danger');
                });
            e.preventDefault();
            return;
        }

        const queueBtn = e.target.closest('[data-queue]');
        if (queueBtn) {
            fetch('/api/track/' + queueBtn.dataset.queue)
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!data.ok) throw new Error(data.message || '');
                    enqueue(data.item, data);
                })
                .catch(function () { if (typeof toast === 'function') toast('Could not add this track to the queue.', 'danger'); });
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
                        if (typeof toast === 'function') toast('This playlist has no playable tracks yet.', 'danger');
                        return;
                    }
                    buildLinearQueue(items, 0, data.items[0] && data.items[0].playlist_title ? data.items[0].playlist_title : 'Playlist');
                })
                .catch(function () { if (typeof toast === 'function') toast('Could not load this playlist.', 'danger'); });
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
                    .catch(function () { if (typeof toast === 'function') toast('Could not load this playlist.', 'danger'); });
            } else {
                fetch('/api/track/' + id)
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (!data.ok) throw new Error(data.message || '');
                        Player.queue = [{
                            id: data.item.id,
                            display: data.item.filename
                                ? stem(data.item.filename) : 'Track',
                            format: data.item.format,
                            path: data.item.output_path || '',
                        }];
                        Player.index = 0;
                        Player.contextLabel = '';
                        saveQueue();
                        renderQueue();
                        loadCurrent(data);
                        play();
                    })
                    .catch(function () { if (typeof toast === 'function') toast('Could not play this track.', 'danger'); });
            }
            e.preventDefault();
        }
    }

    // -------------------------------------------- sleep modes ----

    function setSleepTimer(value) {
        if (sleepTimer) {
            clearTimeout(sleepTimer);
            sleepTimer = null;
        }
        if (value === 'end-track' || value === 'end-queue') {
            Player.sleepMode = value;
            persistSleepMode();
            return;
        }
        Player.sleepMode = null;
        persistSleepMode();
        const minutes = Number(value) || 0;
        if (minutes > 0) {
            sleepTimer = setTimeout(() => {
                sleepStop();
            }, minutes * 60 * 1000);
        }
    }

    function sleepStop() {
        if (sleepTimer) {
            clearTimeout(sleepTimer);
            sleepTimer = null;
        }
        Player.sleepMode = null;
        persistSleepMode();
        if (Player.ui.sleep) Player.ui.sleep.value = '0';
        fadeOutAndPause();
    }

    function persistSleepMode() {
        try {
            localStorage.setItem('appPlayerSleepMode', Player.sleepMode || '');
        } catch (err) { /* private mode */ }
    }

    function restoreSleepMode() {
        try {
            const mode = localStorage.getItem('appPlayerSleepMode') || '';
            if ((mode === 'end-track' || mode === 'end-queue') && Player.ui.sleep) {
                Player.sleepMode = mode;
                Player.ui.sleep.value = mode;
            }
        } catch (err) { /* private mode */ }
    }

    // -------------------------------------------- crossfade ----

    function baseVolume() {
        const v = Player.ui.volume ? Number(Player.ui.volume.value) : 80;
        return Math.max(0, Math.min(1, v / 100));
    }

    function restoreCrossfade() {
        try {
            const secs = Number(localStorage.getItem('appPlayerCrossfade')) || 0;
            Player.crossfade = secs;
            const sel = $('appPlayerCrossfade');
            if (sel) sel.value = String(secs);
        } catch (err) { /* private mode */ }
    }

    // Overlap into `item` when this is an automatic advance: the second
    // element starts the next track at zero volume while the primary
    // fades out, then the primary takes over mid-stream. Returns true
    // when the fade started (caller must not also load normally).
    function startCrossfade(item) {
        const secs = Number(Player.crossfade) || 0;
        const from = Player.audio;
        const to = Player.audio2;
        if (!secs || Player.fading || !from || !to) return false;
        if (from.paused || !from.duration || !Number.isFinite(from.duration)) return false;
        if (isTheaterOpen() || isVideoFormat(item.format)) return false;
        ensureEQ();
        const target = baseVolume();
        to.playbackRate = from.playbackRate || 1;
        to.volume = 0;
        to.src = '/audio/' + item.id;
        try { to.load(); } catch (err) {}
        const playP = to.play();
        if (playP && playP.catch) playP.catch(function () {});
        Player.fading = true;
        Player.fadeState = { to: to, item: item, target: target };
        const fromVol = from.volume;
        const stepMs = 100;
        const ticks = Math.max(1, Math.round((secs * 1000) / stepMs));
        let n = 0;
        Player.fadeTimer = setInterval(function () {
            n += 1;
            const k = Math.min(1, n / ticks);
            try {
                from.volume = Math.max(0, fromVol * (1 - k));
                to.volume = target * k;
            } catch (err) {}
            if (k >= 1) finishFadeNow();
        }, stepMs);
        renderQueue();
        return true;
    }

    function finishFadeNow() {
        if (Player.fadeTimer) {
            try { clearInterval(Player.fadeTimer); } catch (err) {}
            Player.fadeTimer = null;
        }
        const state = Player.fadeState;
        Player.fading = false;
        Player.fadeState = null;
        if (!state) return;
        const from = Player.audio;
        const to = state.to;
        const pos = to.currentTime || 0;
        try { to.pause(); } catch (err) {}
        try { from.pause(); } catch (err) {}
        from.volume = state.target;
        if (to.src) {
            from.src = to.src;
            try { from.load(); } catch (err) {}
            if (pos > 0.5) {
                try { from.currentTime = pos; } catch (err) {}
            }
        }
        try { to.removeAttribute('src'); to.load(); } catch (err) {}
        renderQueue();
    }

    // -------------------------------------------- resume ----

    function posMap() {
        try {
            const map = JSON.parse(localStorage.getItem('appPlayerPos') || '{}');
            return map && typeof map === 'object' ? map : {};
        } catch (err) { return {}; }
    }

    function currentTrackId() {
        const item = Player.queue[Player.index];
        return item ? item.id : 0;
    }

    function savePosition() {
        const id = currentTrackId();
        if (!id || !Player.audio.duration || !Number.isFinite(Player.audio.duration)) return;
        const pos = Player.audio.currentTime;
        if (pos < 5 || pos > Player.audio.duration - 10) return;
        try {
            const map = posMap();
            map[id] = Math.floor(pos);
            const keys = Object.keys(map);
            if (keys.length > 200) {
                keys.slice(0, keys.length - 200).forEach(function (k) { delete map[k]; });
            }
            localStorage.setItem('appPlayerPos', JSON.stringify(map));
        } catch (err) { /* private mode */ }
    }

    function clearPosition(id) {
        const key = id || currentTrackId();
        if (!key) return;
        try {
            const map = posMap();
            if (map[key] !== undefined) {
                delete map[key];
                localStorage.setItem('appPlayerPos', JSON.stringify(map));
            }
        } catch (err) { /* private mode */ }
    }

    function maybeResume() {
        const id = Player.expectResume;
        Player.expectResume = 0;
        if (!id) return;
        let saved = 0;
        try { saved = Number(posMap()[id]) || 0; } catch (err) {}
        if (saved > 10 && Player.audio.duration && Number.isFinite(Player.audio.duration)
                && saved < Player.audio.duration - 15) {
            try { Player.audio.currentTime = saved; } catch (err) {}
            if (typeof toast === 'function') {
                toast('Resumed from ' + fmtTime(saved) + '.', 'info');
            }
        }
    }

    function theaterTrackId() {
        return Player.theaterId || 0;
    }

    function saveTheaterPosition() {
        const video = $('appTheaterVideo');
        const id = theaterTrackId();
        if (!video || !id || !video.duration || !Number.isFinite(video.duration)) return;
        const pos = video.currentTime;
        if (pos < 5 || pos > video.duration - 10) return;
        try {
            const map = posMap();
            map['v' + id] = Math.floor(pos);
            localStorage.setItem('appPlayerPos', JSON.stringify(map));
        } catch (err) { /* private mode */ }
    }

    function onTheaterLoaded() {
        loadAudioTracks();
        const video = $('appTheaterVideo');
        const id = theaterTrackId();
        if (!video || !id) return;
        let saved = 0;
        try { saved = Number(posMap()['v' + id]) || 0; } catch (err) {}
        if (saved > 10 && video.duration && Number.isFinite(video.duration)
                && saved < video.duration - 15) {
            try { video.currentTime = saved; } catch (err) {}
            if (typeof toast === 'function') {
                toast('Resumed from ' + fmtTime(saved) + '.', 'info');
            }
        }
    }

    function onTheaterTime() {
        const video = $('appTheaterVideo');
        if (!video || !video.duration || !Number.isFinite(video.duration)) return;
        if (!video._posTick || Date.now() - video._posTick > 10000) {
            video._posTick = Date.now();
            saveTheaterPosition();
        }
        highlightChapter(video.currentTime);
        Lyrics.highlight(video.currentTime);
    }

    // -------------------------------------------- media session ----

    function initMediaSession() {
        let ms = null;
        try { ms = navigator.mediaSession || null; } catch (err) {}
        if (!ms) return;
        try {
            ms.setActionHandler('play', function () { play(); });
            ms.setActionHandler('pause', function () { pauseAll(); });
            ms.setActionHandler('previoustrack', function () { prev(); });
            ms.setActionHandler('nexttrack', function () { next(); });
            ms.setActionHandler('seekto', function (details) {
                if (details && typeof details.seekTime === 'number') {
                    const video = $('appTheaterVideo');
                    if (isTheaterOpen() && video && video.duration) {
                        video.currentTime = Math.max(0, Math.min(video.duration, details.seekTime));
                    } else {
                        seekTo(details.seekTime / (Player.audio.duration || 1));
                    }
                }
            });
        } catch (err) { /* unsupported action */ }
    }

    function updateMediaSession(item, data) {
        let ms = null;
        try { ms = navigator.mediaSession || null; } catch (err) {}
        if (!ms) return;
        try {
            const title = (data.meta && data.meta.title) || item.display || 'Track';
            const artist = (data.meta && data.meta.artist) || '';
            ms.metadata = new MediaMetadata({
                title: title,
                artist: artist,
                album: Player.contextLabel || '',
                artwork: [{
                    src: location.origin + '/api/cover/' + item.id + '?size=thumb',
                    sizes: '160x160',
                    type: 'image/jpeg',
                }],
            });
        } catch (err) { /* ignore */ }
    }

    let lastPosState = 0;

    function updatePositionState() {
        let ms = null;
        try { ms = navigator.mediaSession || null; } catch (err) {}
        if (!ms || !ms.setPositionState) return;
        if (!Player.audio.duration || !Number.isFinite(Player.audio.duration)) return;
        if (Date.now() - lastPosState < 5000 && !Player.audio.paused) return;
        lastPosState = Date.now();
        try {
            ms.setPositionState({
                duration: Player.audio.duration,
                playbackRate: Player.audio.playbackRate || 1,
                position: Math.min(Player.audio.currentTime, Player.audio.duration),
            });
        } catch (err) { /* ignore */ }
    }

    // -------------------------------------------- chapters ----

    var theaterChapters = [];

    function chapterJump(direction) {
        const video = $('appTheaterVideo');
        if (!video || !theaterChapters.length) return;
        const pos = video.currentTime || 0;
        let target = null;
        if (direction < 0) {
            for (let i = theaterChapters.length - 1; i >= 0; i--) {
                if (theaterChapters[i].t < pos - 2) {
                    target = theaterChapters[i].t;
                    break;
                }
            }
            if (target === null) target = 0;
        } else {
            for (let i = 0; i < theaterChapters.length; i++) {
                if (theaterChapters[i].t > pos + 1) {
                    target = theaterChapters[i].t;
                    break;
                }
            }
            if (target === null && video.duration) target = video.duration;
        }
        if (target !== null) {
            try { video.currentTime = target; } catch (err) {}
        }
    }

    function renderChapters(itemId) {
        const box = $('appTheaterChapters');
        if (!box) return;
        box.innerHTML = '';
        box.classList.add('d-none');
        fetch('/api/chapters/' + itemId)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                const chapters = (data && data.chapters) || [];
                if (!chapters.length) return;
                theaterChapters = chapters.map(function (ch) {
                    return { t: Number(ch.start) || 0, title: ch.title || '' };
                });
                const nav = document.createElement('div');
                nav.className = 'app-chapter-nav';
                const prevBtn = document.createElement('button');
                prevBtn.type = 'button';
                prevBtn.className = 'app-chapter-btn';
                prevBtn.textContent = '\u23EE Prev chapter';
                prevBtn.title = 'Previous chapter';
                prevBtn.addEventListener('click', function () { chapterJump(-1); });
                const nextBtn = document.createElement('button');
                nextBtn.type = 'button';
                nextBtn.className = 'app-chapter-btn';
                nextBtn.textContent = 'Next chapter \u23ED';
                nextBtn.title = 'Next chapter';
                nextBtn.addEventListener('click', function () { chapterJump(1); });
                nav.appendChild(prevBtn);
                nav.appendChild(nextBtn);
                box.appendChild(nav);
                chapters.forEach(function (ch) {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'app-chapter-btn';
                    btn.dataset.start = String(ch.start);
                    const label = (ch.title ? ch.title + ' · ' : '') + fmtTime(ch.start);
                    btn.textContent = label;
                    btn.title = label;
                    btn.addEventListener('click', function () {
                        const video = $('appTheaterVideo');
                        if (video) {
                            try { video.currentTime = Number(ch.start) || 0; } catch (err) {}
                        }
                    });
                    box.appendChild(btn);
                });
                box.classList.remove('d-none');
            })
            .catch(function () {});
    }

    function highlightChapter(pos) {
        const box = $('appTheaterChapters');
        if (!box || box.classList.contains('d-none')) return;
        Array.from(box.children).forEach(function (btn) {
            const next = btn.nextElementSibling;
            const start = Number(btn.dataset.start) || 0;
            const end = next ? Number(next.dataset.start) : Infinity;
            btn.classList.toggle('active', pos >= start && pos < end);
        });
    }

    // -------------------------------------------- queue m3u ----

    function fillQueuePaths() {
        const missing = Player.queue.some(function (item) { return !item.path; });
        if (!missing) return Promise.resolve();
        return fetch('/api/library')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                const byId = {};
                (data.items || []).forEach(function (it) {
                    byId[it.id] = it.output_path || '';
                });
                Player.queue.forEach(function (item) {
                    if (!item.path && byId[item.id]) item.path = byId[item.id];
                });
                saveQueue();
            })
            .catch(function () {});
    }

    function exportQueue() {
        if (!Player.queue.length) {
            if (typeof toast === 'function') toast('The queue is empty.', 'info');
            return;
        }
        fillQueuePaths().then(function () {
            const lines = ['#EXTM3U'];
            Player.queue.forEach(function (item) {
                lines.push('#EXTINF:-1,' + item.display.replace(/\r?\n/g, ' '));
                lines.push(item.path || ('/audio/' + item.id));
            });
            const blob = new Blob([lines.join('\n') + '\n'], { type: 'audio/x-mpegurl' });
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'queue.m3u';
            document.body.appendChild(a);
            a.click();
            setTimeout(function () {
                URL.revokeObjectURL(a.href);
                a.remove();
            }, 1000);
        });
    }

    function importQueueFile(file) {
        const reader = new FileReader();
        reader.onload = function () {
            const paths = String(reader.result || '').split(/\r?\n/)
                .map(function (l) { return l.trim(); })
                .filter(function (l) { return l && l.charAt(0) !== '#'; });
            if (!paths.length) {
                if (typeof toast === 'function') toast('No tracks found in that file.', 'info');
                return;
            }
            fetch('/api/library')
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    const items = data.items || [];
                    const base = function (p) {
                        const parts = String(p).split(/[\\/]/);
                        return parts[parts.length - 1].toLowerCase();
                    };
                    let added = 0;
                    const matches = [];
                    paths.forEach(function (p) {
                        const hit = items.find(function (it) {
                            return (it.output_path && it.output_path === p)
                                || (it.output_path && base(it.output_path) === base(p));
                        }) || items.find(function (it) {
                            return it.id && ('/audio/' + it.id) === p;
                        });
                        if (hit) matches.push(hit);
                    });
                    if (!matches.length) {
                        if (typeof toast === 'function') toast('None of those files are in the library.', 'info');
                        return;
                    }
                    if (Player.queue.length === 0) {
                        buildLinearQueue(matches, 0, 'Imported playlist');
                    } else {
                        matches.forEach(function (m) { enqueue(m); });
                    }
                    added = matches.length;
                    if (typeof toast === 'function') {
                        toast('Added ' + added + ' of ' + paths.length + ' tracks.', 'info');
                    }
                })
                .catch(function () {
                    if (typeof toast === 'function') toast('Could not read the library.', 'info');
                });
        };
        reader.readAsText(file);
    }

    // -------------------------------------------- lyrics ----

    // Lyrics overlay with synced-line highlighting. Lookup is on demand
    // (button click), served by /api/lyrics, and follows whichever side
    // is playing: the bottom bar or the theater video.
    const Lyrics = {
        itemId: 0,
        lines: [],
        cacheGet: function (itemId) {
            try {
                const cache = JSON.parse(localStorage.getItem('appLyricsCache') || '{}');
                const entry = cache[itemId];
                if (!entry) return null;
                if (Date.now() - (entry.saved || 0) > 30 * 86400000) return null;
                return entry.data || null;
            } catch (err) {
                return null;
            }
        },
        cachePut: function (itemId, data) {
            try {
                const cache = JSON.parse(localStorage.getItem('appLyricsCache') || '{}');
                cache[itemId] = { saved: Date.now(), data: data };
                const keys = Object.keys(cache);
                if (keys.length > 50) delete cache[keys[0]];
                localStorage.setItem('appLyricsCache', JSON.stringify(cache));
            } catch (err) { /* private mode or quota */ }
        },
        open: function (itemId) {
            const overlay = $('appLyrics');
            const body = $('appLyricsBody');
            if (!overlay || !body || !itemId) return;
            Lyrics.itemId = itemId;
            Lyrics.lines = [];
            $('appLyricsTitle').textContent = 'Lyrics';
            $('appLyricsArtist').textContent = '';
            body.innerHTML = '';
            const loading = document.createElement('span');
            loading.className = 'text-muted small';
            loading.textContent = 'Loading…';
            body.appendChild(loading);
            overlay.classList.remove('d-none');
            const cached = Lyrics.cacheGet(itemId);
            const render = function (data) {
                Lyrics.cachePut(itemId, data);
                if (!data.ok) throw new Error('');
                    $('appLyricsTitle').textContent =
                        (data.title && data.artist) ? data.title + ' · ' + data.artist
                        : (data.title || 'Lyrics');
                    $('appLyricsArtist').textContent =
                        (data.title && data.artist) ? '' : (data.artist || '');
                    body.innerHTML = '';
                    if (data.synced && data.synced.length) {
                        Lyrics.lines = data.synced;
                        data.synced.forEach(function (line, i) {
                            const div = document.createElement('div');
                            div.className = 'app-lyric-line';
                            div.dataset.t = String(line.t);
                            div.textContent = line.text;
                            div.title = fmtTime(line.t) + ' — click to jump';
                            div.addEventListener('click', function () {
                                Lyrics.seek(line.t);
                            });
                            body.appendChild(div);
                        });
                        Lyrics.highlight(Lyrics.position());
                    } else if (data.plain) {
                        const pre = document.createElement('div');
                        pre.className = 'app-lyric-plain';
                        pre.textContent = data.plain;
                        body.appendChild(pre);
                    } else {
                        const none = document.createElement('span');
                        none.className = 'text-muted small';
                        none.textContent = 'No lyrics found for this track.';
                        body.appendChild(none);
                    }
                };
            if (cached && cached.ok) {
                render(cached);
            } else {
                fetch('/api/lyrics/' + itemId)
                    .then(function (r) { return r.json(); })
                    .then(render)
                    .catch(function () {
                        body.innerHTML = '';
                        const none = document.createElement('span');
                        none.className = 'text-muted small';
                        none.textContent = 'Could not load lyrics.';
                        body.appendChild(none);
                    });
            }
        },
        close: function () {
            const overlay = $('appLyrics');
            if (overlay) overlay.classList.add('d-none');
            Lyrics.itemId = 0;
            Lyrics.lines = [];
        },
        position: function () {
            const video = $('appTheaterVideo');
            if (isTheaterOpen() && video && video.duration) return video.currentTime;
            if (Player.audio.duration) return Player.audio.currentTime;
            return 0;
        },
        seek: function (t) {
            const video = $('appTheaterVideo');
            if (isTheaterOpen() && video && video.duration) {
                try { video.currentTime = t; } catch (err) {}
            } else if (Player.audio.duration) {
                try { Player.audio.currentTime = Math.min(t, Player.audio.duration - 1); } catch (err) {}
            }
        },
        highlight: function (pos) {
            if (!Lyrics.itemId || !Lyrics.lines.length) return;
            const body = $('appLyricsBody');
            if (!body || !body.children.length) return;
            let active = 0;
            for (let i = 0; i < Lyrics.lines.length; i++) {
                if (pos >= Lyrics.lines[i].t) active = i;
                else break;
            }
            Array.from(body.children).forEach(function (el, i) {
                el.classList.toggle('active', i === active);
            });
            const current = body.children[active];
            if (current && current.scrollIntoView) {
                current.scrollIntoView({ block: 'nearest' });
            }
        },
    };

    function currentLyricId() {
        if (isTheaterOpen() && theaterTrackId()) return theaterTrackId();
        return currentTrackId();
    }

    // -------------------------------------------- notices ----

    function csrfHeader() {
        let token = '';
        try {
            const meta = document.querySelector('meta[name="csrf-token"]');
            token = meta ? meta.getAttribute('content') : '';
        } catch (err) {}
        return { 'X-CSRFToken': token };
    }

    function postNotice(action, clear) {
        return fetch('/api/notices/read' + (clear ? '?clear=1' : ''), {
            method: 'POST',
            headers: csrfHeader(),
        }).then(function (r) { return r.json(); }).catch(function () { return {}; });
    }

    function refreshBell() {
        const dot = $('appBellDot');
        if (!dot) return;
        fetch('/api/notices')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                dot.classList.toggle('d-none', !(data.unread > 0));
            })
            .catch(function () {});
    }

    function toggleNotices() {
        const panel = $('appNotices');
        if (!panel) return;
        if (panel.classList.contains('d-none')) {
            loadNotices();
            panel.classList.remove('d-none');
            postNotice('read', false).then(refreshBell);
        } else {
            panel.classList.add('d-none');
        }
    }

    function loadNotices() {
        const body = $('appNoticesBody');
        if (!body) return;
        body.innerHTML = '';
        fetch('/api/notices')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                const items = data.items || [];
                if (!items.length) {
                    body.innerHTML = '<span class="text-muted small">Nothing yet — finished and failed downloads land here.</span>';
                    return;
                }
                items.forEach(function (n) {
                    const wrap = document.createElement('div');
                    wrap.className = 'app-notice' + (n.read ? '' : ' unread');
                    const title = document.createElement('div');
                    title.className = 'app-notice-title';
                    title.textContent = n.title || 'Notice';
                    wrap.appendChild(title);
                    if (n.body) {
                        const text = document.createElement('div');
                        text.className = 'app-notice-body text-muted small';
                        text.textContent = n.body;
                        wrap.appendChild(text);
                    }
                    body.appendChild(wrap);
                });
            })
            .catch(function () {
                body.innerHTML = '<span class="text-muted small">Could not load.</span>';
            });
    }

    function toggleMute() {
        const video = $('appTheaterVideo');
        if (isTheaterOpen() && video) {
            try { video.muted = !video.muted; } catch (err) {}
            updateMuteIcon();
            return;
        }
        if (!Player.audio) return;
        if (Player.audio.volume > 0) {
            Player.mutedVol = Player.audio.volume;
            Player.audio.volume = 0;
            if (Player.audio2) Player.audio2.volume = 0;
            if (Player.ui.volume) Player.ui.volume.value = 0;
        } else {
            const v = Player.mutedVol != null ? Player.mutedVol : 0.8;
            Player.audio.volume = v;
            if (Player.audio2) Player.audio2.volume = v;
            if (Player.ui.volume) Player.ui.volume.value = Math.round(v * 100);
        }
        updateMuteIcon();
    }

    function updateMuteIcon() {
        const btn = $('appPlayerMute');
        if (!btn) return;
        const video = $('appTheaterVideo');
        if (isTheaterOpen() && video) {
            let muted = false;
            try { muted = !!video.muted; } catch (err) {}
            btn.innerHTML = muted ? '&#128263;' : '&#128266;';
            btn.title = muted ? 'Unmute (M)' : 'Mute (M)';
            btn.setAttribute('aria-label', muted ? 'Unmute' : 'Mute');
            return;
        }
        if (!Player.audio) return;
        const muted = Player.audio.volume === 0;
        btn.innerHTML = muted ? '&#128263;' : '&#128266;';
        btn.title = muted ? 'Unmute (M)' : 'Mute (M)';
        btn.setAttribute('aria-label', muted ? 'Unmute' : 'Mute');
    }

    // -------------------------------------------- lightbox ----

    function openLightbox(src) {
        closeLightbox();
        if (!src) return;
        const overlay = document.createElement('div');
        overlay.className = 'app-lightbox';
        overlay.id = 'appLightbox';
        const img = document.createElement('img');
        img.alt = 'Cover art enlarged';
        img.src = src;
        img.onerror = () => closeLightbox();
        overlay.appendChild(img);
        overlay.addEventListener('click', closeLightbox);
        document.body.appendChild(overlay);
        document.addEventListener('keydown', escLightbox);
    }

    function closeLightbox() {
        const overlay = $('appLightbox');
        if (overlay) overlay.remove();
        document.removeEventListener('keydown', escLightbox);
    }

    function escLightbox(e) {
        if (e.key === 'Escape') closeLightbox();
    }

    window.AudioLightbox = { open: openLightbox, close: closeLightbox };

    // -------------------------------------------- theater picture ----

    const theaterPic = { b: 100, c: 100, s: 100, fit: 'contain', sub: '1em' };

    function restoreTheaterPic() {
        try {
            const saved = JSON.parse(localStorage.getItem('appTheaterPic') || 'null');
            if (saved && typeof saved === 'object') {
                if (saved.b) theaterPic.b = Math.max(50, Math.min(150, Number(saved.b)));
                if (saved.c) theaterPic.c = Math.max(50, Math.min(150, Number(saved.c)));
                if (saved.s != null) theaterPic.s = Math.max(0, Math.min(200, Number(saved.s)));
                if (saved.fit) theaterPic.fit = saved.fit;
                if (saved.sub) theaterPic.sub = saved.sub;
            }
        } catch (err) { /* private mode */ }
        syncPicControls();
        applyTheaterPic();
    }

    function syncPicControls() {
        const map = { appPicBright: theaterPic.b, appPicContrast: theaterPic.c,
                      appPicSaturate: theaterPic.s };
        Object.keys(map).forEach((id) => {
            const el = $(id);
            if (el) el.value = String(map[id]);
        });
        const fit = $('appTheaterFit');
        if (fit) fit.value = theaterPic.fit;
        const sub = $('appTheaterSubSize');
        if (sub) sub.value = theaterPic.sub;
    }

    function applyTheaterPic() {
        const video = $('appTheaterVideo');
        if (video) {
            video.style.filter = 'brightness(' + (theaterPic.b / 100) + ') ' +
                'contrast(' + (theaterPic.c / 100) + ') ' +
                'saturate(' + (theaterPic.s / 100) + ')';
            video.style.objectFit = theaterPic.fit;
            try {
                video.style.setProperty('--cue-size', theaterPic.sub);
            } catch (err) { /* ignore */ }
        }
        try {
            localStorage.setItem('appTheaterPic', JSON.stringify(theaterPic));
        } catch (err) { /* private mode */ }
    }

    function snapshotFrame() {
        const video = $('appTheaterVideo');
        if (!video || !video.videoWidth) {
            if (typeof toast === 'function') toast('No video frame yet.', 'info');
            return;
        }
        try {
            const canvas = document.createElement('canvas');
            canvas.width = video.videoWidth;
            canvas.height = video.videoHeight;
            canvas.getContext('2d').drawImage(video, 0, 0);
            canvas.toBlob(function (blob) {
                if (!blob) {
                    if (typeof toast === 'function') toast('Snapshot failed.', 'danger');
                    return;
                }
                const a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                const title = ($('appTheaterTitle') || {}).textContent || 'frame';
                a.download = title.replace(/[^\w\- ]+/g, '').trim().slice(0, 60) + '.png';
                document.body.appendChild(a);
                a.click();
                setTimeout(function () {
                    URL.revokeObjectURL(a.href);
                    a.remove();
                }, 1000);
            }, 'image/png');
        } catch (err) {
            if (typeof toast === 'function') toast('Snapshot failed.', 'danger');
        }
    }

    function loadAudioTracks() {
        const sel = $('appTheaterAudio');
        const video = $('appTheaterVideo');
        if (!sel || !video) return;
        sel.innerHTML = '';
        sel.classList.add('d-none');
        let tracks = null;
        try {
            tracks = video.audioTracks || null;
        } catch (err) {
            tracks = null;
        }
        if (!tracks || tracks.length < 2) return;
        for (let i = 0; i < tracks.length; i++) {
            const opt = document.createElement('option');
            opt.value = String(i);
            const label = tracks[i].label || tracks[i].language || ('Track ' + (i + 1));
            opt.textContent = (tracks[i].language ? tracks[i].language + ' — ' : '') + label;
            if (tracks[i].enabled) opt.selected = true;
            sel.appendChild(opt);
        }
        sel.classList.remove('d-none');
        sel.onchange = () => {
            const idx = Number(sel.value) || 0;
            for (let i = 0; i < tracks.length; i++) {
                try { tracks[i].enabled = (i === idx); } catch (err) {}
            }
        };
    }

    document.addEventListener('DOMContentLoaded', init);

    // Small public surface for the Player tab (transport mirror, queue
    // management, visualizer). The bottom bar remains the single owner of
    // playback state; these just drive it.
    window.AudioPlayer = {
        toggle: togglePlay,
        next: next,
        prev: prev,
        enqueue: function (item) {
            enqueue(item);
        },
        playIndex: function (i) {
            if (i >= 0 && i < Player.queue.length) {
                Player.index = i;
                saveQueue();
                loadCurrent();
            }
        },
        removeAt: removeFromQueue,
        importFile: function (file) {
            importQueueFile(file);
        },
        playNext: function (item) {
            playNext(item);
        },
        playItems: function (items, startIndex, label) {
            buildLinearQueue(items, startIndex || 0, label || '');
        },
        snapshot: function () {
            return {
                queue: Player.queue.slice(),
                index: Player.index,
                context: Player.contextLabel,
                repeat: Player.repeat,
            };
        },
        lyrics: function () {
            const id = currentLyricId();
            if (id) Lyrics.open(id);
        },
        closeLyrics: function () {
            Lyrics.close();
        },
        getAnalyser: function () {
            return (typeof eqNodes !== 'undefined' && eqNodes) ? eqNodes.analyser || null : null;
        },
    };

    // Keyboard shortcuts: Space toggles, arrows seek/volume. Ignored while
    // typing in a field so search boxes and forms keep working.
    document.addEventListener('keydown', (e) => {
        const tag0 = (e.target && e.target.tagName) || '';
        const typing0 = /INPUT|TEXTAREA|SELECT/.test(tag0)
            || (e.target && e.target.isContentEditable);
        if (!typing0 && e.key === '?') {
            const sc = $('appShortcuts');
            if (sc) sc.classList.toggle('d-none');
            return;
        }
        if (!Player.audio || Player.ui.player.classList.contains('d-none')) return;
        const target = e.target || {};
        const tag = target.tagName || '';
        if (/INPUT|TEXTAREA|SELECT/.test(tag) || target.isContentEditable) return;
        if (e.code === 'Space') {
            e.preventDefault();
            togglePlay();
        } else if (e.key === 'n' || e.key === 'N') {
            next();
        } else if (e.key === 'p' || e.key === 'P') {
            prev();
        } else if (e.key === 'm' || e.key === 'M') {
            toggleMute();
        } else if (e.key === 'l' || e.key === 'L') {
            const id = currentLyricId();
            if (id) Lyrics.open(id);
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
        if (Player.audio2) Player.audio2.volume = v / 100;
        localStorage.setItem('appPlayerVolume', String(v));
        updateMuteIcon();
    }
})();