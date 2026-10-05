function copyPath(buttonId, statusId) {
    const statusEl = document.getElementById(statusId || 'copyStatus');
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(document.getElementById('output_path').value)
            .then(() => showCopyStatus(statusEl))
            .catch(() => fallbackCopy(statusEl));
    } else {
        fallbackCopy(statusEl);
    }
}

function fallbackCopy(statusEl) {
    const input = document.getElementById('output_path');
    if (input) {
        input.select();
        document.execCommand('copy');
        showCopyStatus(statusEl);
    }
}

function showCopyStatus(statusEl) {
    if (statusEl) {
        statusEl.textContent = 'Path copied to clipboard!';
        setTimeout(() => { statusEl.textContent = ''; }, 3000);
    }
}

// --- Recent conversions: saved-file cell + copy/reveal helpers -----------

// Renders the "File / Saved to" cell for a completed conversion, mirroring
// the server-rendered markup so live-polled rows match the initial page.
function isVideoItem(item) {
    return /video$/i.test(item.format || '');
}

function renderSavedFileCell(cell, item) {
    cell.innerHTML = '';

    if (isVideoItem(item)) {
        const thumb = document.createElement('img');
        thumb.className = 'file-thumb';
        thumb.alt = '';
        thumb.loading = 'lazy';
        thumb.src = '/api/cover/' + item.id + '?size=thumb';
        thumb.onerror = function () { thumb.remove(); };
        cell.appendChild(thumb);
    }

    const actions = document.createElement('div');
    actions.className = 'd-flex flex-wrap gap-1 align-items-center file-actions';

    const play = document.createElement('button');
    play.type = 'button';
    play.className = 'btn btn-sm btn-primary play-btn';
    play.title = 'Play in app';
    play.setAttribute('data-play', item.id);
    play.textContent = '\u25B6';
    actions.appendChild(play);

    const dl = document.createElement('a');
    dl.className = 'btn btn-sm btn-success';
    dl.href = '/download/' + item.id;
    dl.textContent = 'Download';
    actions.appendChild(dl);

    if (item.output_path) {
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.className = 'btn btn-sm btn-outline-secondary copy-path-btn';
        copy.title = 'Copy full path';
        copy.setAttribute('data-copy-path', item.output_path);
        copy.textContent = 'Copy';
        actions.appendChild(copy);

        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'btn btn-sm btn-outline-secondary reveal-btn';
        open.title = 'Show in file manager';
        open.setAttribute('data-reveal', item.id);
        open.textContent = 'Open folder';
        actions.appendChild(open);

        const queue = document.createElement('button');
        queue.type = 'button';
        queue.className = 'btn btn-sm btn-outline-secondary queue-btn';
        queue.title = 'Add to player queue';
        queue.setAttribute('data-queue', item.id);
        queue.textContent = '+ Queue';
        actions.appendChild(queue);

        const nextBtn = document.createElement('button');
        nextBtn.type = 'button';
        nextBtn.className = 'btn btn-sm btn-outline-secondary queue-next-btn';
        nextBtn.title = 'Play next (insert after current track)';
        nextBtn.setAttribute('data-queue-next', item.id);
        nextBtn.textContent = 'Next \u2192';
        actions.appendChild(nextBtn);

        const rename = document.createElement('button');
        rename.type = 'button';
        rename.className = 'btn btn-sm btn-outline-secondary rename-btn';
        rename.title = 'Rename file';
        rename.setAttribute('data-rename', item.id);
        rename.setAttribute('data-name', item.output_path.split(/[\\/]/).pop());
        rename.textContent = '✎';
        actions.appendChild(rename);

        const tags = document.createElement('button');
        tags.type = 'button';
        tags.className = 'btn btn-sm btn-outline-secondary tags-btn';
        tags.title = 'Edit title, artist and album';
        tags.setAttribute('data-tags', item.id);
        tags.textContent = 'Tags';
        actions.appendChild(tags);

        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'btn btn-sm btn-outline-danger delete-btn';
        del.title = 'Delete file from disk and remove from history';
        del.setAttribute('data-delete', item.id);
        del.textContent = 'Delete';
        actions.appendChild(del);

        const meta = document.createElement('div');
        meta.className = 'small text-muted file-path mt-1';
        meta.title = item.output_path;

        const name = document.createElement('span');
        name.className = 'fw-semibold file-name';
        name.textContent = item.output_path.split(/[\\/]/).pop();
        meta.appendChild(name);
        if (item.quality) {
            const qual = document.createElement('span');
            qual.className = 'file-quality d-block';
            qual.textContent = item.quality;
            qual.title = 'Source quality';
            meta.appendChild(qual);
        }

        const full = document.createElement('span');
        full.className = 'file-full d-block';
        full.textContent = item.output_path;
        meta.appendChild(full);

        cell.appendChild(actions);
        cell.appendChild(meta);
    } else {
        cell.appendChild(actions);
    }
}

function copyTextToClipboard(text, btn) {
    const flashCopied = () => {
        const original = btn.textContent;
        btn.textContent = 'Copied!';
        btn.disabled = true;
        setTimeout(() => {
            btn.textContent = original;
            btn.disabled = false;
        }, 1500);
    };
    const fallbackTextarea = () => {
        const area = document.createElement('textarea');
        area.value = text;
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        document.execCommand('copy');
        document.body.removeChild(area);
        flashCopied();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text)
            .then(flashCopied)
            .catch(fallbackTextarea);
    } else {
        fallbackTextarea();
    }
}

// Delegated handlers: a single listener covers server-rendered rows and the
// rows that renderSavedFileCell() injects after a conversion finishes.
function csrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
}

function toast(message, kind) {
    let box = document.getElementById('toastBox');
    if (!box) {
        box = document.createElement('div');
        box.id = 'toastBox';
        box.className = 'toast-box';
        box.setAttribute('aria-live', 'polite');
        document.body.appendChild(box);
    }
    const el = document.createElement('div');
    el.className = 'toast-msg toast-' + (kind || 'info');
    el.textContent = message;
    box.appendChild(el);
    setTimeout(() => {
        el.classList.add('toast-out');
        setTimeout(() => el.remove(), 400);
    }, 4500);
}

document.addEventListener('click', (e) => {
    const subBtn = e.target.closest('[data-subscribe]');
    if (subBtn) {
        subBtn.disabled = true;
        postJSON('/api/subscribe/' + subBtn.dataset.subscribe, {})
            .then((data) => {
                toast(data.message || (data.ok ? 'Following playlist.' : 'Could not follow.'),
                      data.ok ? 'success' : 'danger');
                subBtn.disabled = false;
                loadSubscriptions();
            })
            .catch(() => {
                toast('Could not follow this playlist.', 'danger');
                subBtn.disabled = false;
            });
        e.preventDefault();
        return;
    }
    const topBtn = e.target.closest('[data-top]');
    if (topBtn) {
        topBtn.disabled = true;
        fetch('/api/queue/top/' + topBtn.dataset.top, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken() },
        })
            .then((r) => r.json())
            .then((data) => {
                toast(data.ok ? (data.message || 'Moved to the top.') : (data.message || 'Could not move.'), data.ok ? 'success' : 'danger');
                if (typeof refreshTable === 'function') refreshTable();
            })
            .catch(() => toast('Could not move.', 'danger'))
            .finally(() => { topBtn.disabled = false; });
        e.preventDefault();
        return;
    }
    const pauseBtn = e.target.closest('[data-pause]');
    if (pauseBtn) {
        pauseBtn.disabled = true;
        fetch('/api/pause/' + pauseBtn.dataset.pause)
            .then((r) => r.json())
            .then((data) => {
                if (!data.ok) {
                    if (data.message) toast(data.message, 'danger');
                    pauseBtn.disabled = false;
                    return;
                }
                toast('Download paused — resume anytime.', 'info');
                if (typeof refreshTable === 'function') refreshTable();
            })
            .catch(() => {
                toast('Could not pause this track.', 'danger');
                pauseBtn.disabled = false;
            });
        e.preventDefault();
        return;
    }
    const resumeBtn = e.target.closest('[data-resume]');
    if (resumeBtn) {
        resumeBtn.disabled = true;
        fetch('/api/resume/' + resumeBtn.dataset.resume)
            .then((r) => r.json())
            .then((data) => {
                if (!data.ok) {
                    if (data.message) toast(data.message, 'danger');
                    resumeBtn.disabled = false;
                    return;
                }
                toast('Download resumed.', 'info');
                if (typeof refreshTable === 'function') refreshTable();
            })
            .catch(() => {
                toast('Could not resume this track.', 'danger');
                resumeBtn.disabled = false;
            });
        e.preventDefault();
        return;
    }
    const skipBtn = e.target.closest('[data-skip]');
    if (skipBtn) {
        skipBtn.disabled = true;
        fetch('/api/skip/' + skipBtn.dataset.skip)
            .then((r) => r.json())
            .then((data) => {
                if (!data.ok) {
                    if (data.message) toast(data.message, 'danger');
                    skipBtn.disabled = false;
                    return;
                }
                const row = skipBtn.closest('tr');
                if (row) {
                    const badge = row.querySelector('.status-badge');
                    if (badge) {
                        badge.textContent = 'skipped';
                        badge.className = 'badge status-badge status-skipped';
                    }
                    const bar = row.querySelector('.row-progress-bar');
                    if (bar) bar.style.width = '100%';
                    const cell = row.querySelector('.row-file');
                    if (cell) cell.innerHTML = '<span class="text-muted small">Skipped</span>';
                }
                toast('Track skipped.', 'info');
                if (typeof refreshTable === 'function') refreshTable();
            })
            .catch(() => {
                toast('Could not skip this track.', 'danger');
                skipBtn.disabled = false;
            });
        e.preventDefault();
        return;
    }
    const delPlBtn = e.target.closest('[data-delete-playlist]');
    if (delPlBtn) {
        if (!confirm('Delete every finished track in this playlist from disk and remove the playlist?')) {
            e.preventDefault();
            return;
        }
        delPlBtn.disabled = true;
        fetch('/api/delete-playlist/' + delPlBtn.dataset.deletePlaylist, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken() },
        })
            .then((r) => r.json())
            .then((data) => {
                if (!data.ok) {
                    if (data.message) toast(data.message, 'danger');
                    delPlBtn.disabled = false;
                    return;
                }
                const row = delPlBtn.closest('tr');
                const box = row && row.parentElement
                    ? row.parentElement.querySelector('#children-' + delPlBtn.dataset.deletePlaylist)
                    : null;
                if (box) box.remove();
                if (row) row.remove();
                toast(data.trashed_files ? ('Moved ' + data.removed_tracks + ' track file(s) to Trash.') : ('Deleted ' + data.removed_tracks + ' track(s).'), 'info');
                refreshStorageInfo();
                if (typeof refreshTable === 'function') refreshTable();
            })
            .catch(() => {
                toast('Could not delete this playlist.', 'danger');
                delPlBtn.disabled = false;
            });
        e.preventDefault();
        return;
    }
    const copyBtn = e.target.closest('[data-copy-path]');
    if (copyBtn) {
        copyTextToClipboard(copyBtn.dataset.copyPath, copyBtn);
        e.preventDefault();
        return;
    }
    const delBtn = e.target.closest('[data-delete]');
    if (delBtn) {
        if (!confirm('Move this file to the Trash and remove it from history?')) {
            e.preventDefault();
            return;
        }
        delBtn.disabled = true;
        fetch('/api/delete/' + delBtn.dataset.delete, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken() },
        })
            .then((r) => r.json())
            .then((data) => {
                if (!data.ok) {
                    if (data.message) toast(data.message, 'danger');
                    delBtn.disabled = false;
                    return;
                }
                const row = delBtn.closest('tr');
                if (row) row.remove();
                toast(data.removed_file ? (data.message || 'Deleted file from disk.') : 'Removed from history.', 'info');
                refreshStorageInfo();
                if (typeof refreshTable === 'function') refreshTable();
            })
            .catch(() => {
                toast('Could not delete this track.', 'danger');
                delBtn.disabled = false;
            });
        e.preventDefault();
        return;
    }
    const retryBtn = e.target.closest('[data-retry]');
    if (retryBtn) {
        retryBtn.disabled = true;
        fetch('/api/retry/' + retryBtn.dataset.retry, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken() },
        })
            .then((r) => r.json())
            .then((data) => {
                if (!data.ok) {
                    if (data.message) toast(data.message, 'danger');
                    retryBtn.disabled = false;
                    return;
                }
                toast('Download re-queued.', 'success');
                if (typeof refreshTable === 'function') refreshTable();
            })
            .catch(() => {
                toast('Could not retry this track.', 'danger');
                retryBtn.disabled = false;
            });
        e.preventDefault();
        return;
    }
    const renameBtn = e.target.closest('[data-rename]');
    if (renameBtn) {
        const current = renameBtn.dataset.name || '';
        const stem = current.replace(/\.[^.]+$/, '');
        const next = prompt('Rename file (extension is kept):', stem);
        if (next === null) {
            e.preventDefault();
            return;
        }
        fetch('/api/rename/' + renameBtn.dataset.rename, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            body: JSON.stringify({ name: next }),
        })
            .then((r) => r.json())
            .then((data) => {
                if (!data.ok) {
                    if (data.message) toast(data.message, 'danger');
                    return;
                }
                toast('Renamed to ' + data.filename + '.', 'success');
                if (typeof refreshTable === 'function') refreshTable();
                else window.location.reload();
            })
            .catch(() => toast('Could not rename this file.', 'danger'));
        e.preventDefault();
        return;
    }
    const tagsBtn = e.target.closest('[data-tags]');
    if (tagsBtn) {
        if (typeof bootstrap === 'undefined') {
            toast('Tag editor unavailable.', 'danger');
            e.preventDefault();
            return;
        }
        const modalEl = document.getElementById('tagsModal');
        if (!modalEl) {
            e.preventDefault();
            return;
        }
        fetch('/api/track/' + tagsBtn.dataset.tags)
            .then((r) => r.json())
            .then((data) => {
                document.getElementById('tagsTitle').value = (data.meta && data.meta.title) || '';
                document.getElementById('tagsArtist').value = (data.meta && data.meta.artist) || '';
                document.getElementById('tagsAlbum').value = (data.meta && data.meta.album) || '';
                const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
                document.getElementById('tagsSave').onclick = () => {
                    fetch('/api/metadata/' + tagsBtn.dataset.tags, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
                        body: JSON.stringify({
                            title: document.getElementById('tagsTitle').value,
                            artist: document.getElementById('tagsArtist').value,
                            album: document.getElementById('tagsAlbum').value,
                        }),
                    })
                        .then((r) => r.json())
                        .then((saved) => {
                            if (!saved.ok) {
                                toast(saved.message || 'Could not save tags.', 'danger');
                                return;
                            }
                            modal.hide();
                            toast('Tags updated.', 'success');
                        })
                        .catch(() => toast('Could not save tags.', 'danger'));
                };
                modal.show();
            })
            .catch(() => toast('Could not load current tags.', 'danger'));
        e.preventDefault();
        return;
    }
    const infoBtn = e.target.closest('[data-info]');
    if (infoBtn) {
        if (typeof bootstrap === 'undefined') {
            toast('Details unavailable.', 'danger');
            e.preventDefault();
            return;
        }
        const modalEl = document.getElementById('detailsModal');
        fetch('/api/details/' + infoBtn.dataset.info)
            .then((r) => r.json())
            .then((data) => {
                if (!data.ok) {
                    toast(data.message || 'Could not read this file.', 'danger');
                    return;
                }
                const bytes = (n) => {
                    if (!n) return '0 B';
                    const units = ['B', 'KB', 'MB', 'GB'];
                    let i = 0;
                    let v = n;
                    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
                    return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + ' ' + units[i];
                };
                const secs = (n) => {
                    if (!n) return '—';
                    const s = Math.round(n);
                    const h = Math.floor(s / 3600);
                    const m = Math.floor((s % 3600) / 60);
                    const r = s % 60;
                    return (h ? h + ':' + String(m).padStart(2, '0') : String(m)) + ':' + String(r).padStart(2, '0');
                };
                const rows = [
                    ['Title', data.tags.title || '—'],
                    ['Artist', data.tags.artist || '—'],
                    ['Album', data.tags.album || '—'],
                    ['Format', (data.format || '—') + (data.playlist ? ' (playlist)' : '')],
                    ['Quality', data.quality || '—'],
                    ['Duration', secs(data.duration)],
                    ['Size', bytes(data.size)],
                    ['Plays', String(data.played)],
                    ['Rating', (data.rating || 0) ? '★'.repeat(Math.min(5, data.rating)) : '—'],
                    ['Status', data.status],
                    ['Added', data.created || '—'],
                    ['Folder', data.folder || '—'],
                    ['File', data.file || '(not on disk)'],
                    ['Source', data.url || '—'],
                ];
                const tbody = modalEl ? modalEl.querySelector('#detailsTable tbody') : null;
                if (tbody) {
                    tbody.innerHTML = rows
                        .map(([k, v]) => '<tr><th class="text-muted" style="width:9rem">' + k +
                            '</th><td class="text-break">' + String(v).replace(/</g, '&lt;') + '</td></tr>')
                        .join('');
                }
                if (!modalEl) return;
                bootstrap.Modal.getOrCreateInstance(modalEl).show();
            })
            .catch(() => toast('Could not load details.', 'danger'));
        e.preventDefault();
        return;
    }
    const extractBtn = e.target.closest('[data-extract]');
    if (extractBtn) {
        extractBtn.disabled = true;
        const original = extractBtn.textContent;
        extractBtn.textContent = '…';
        fetch('/api/extract-audio/' + extractBtn.dataset.extract, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken() },
        })
            .then((r) => r.json())
            .then((data) => {
                if (!data.ok) {
                    toast(data.message || 'Could not extract audio.', 'danger');
                } else {
                    toast('Audio saved as FLAC.', 'success');
                    if (typeof refreshTable === 'function') refreshTable();
                    else window.location.reload();
                }
            })
            .catch(() => toast('Could not extract audio.', 'danger'))
            .finally(() => {
                extractBtn.disabled = false;
                extractBtn.textContent = original;
            });
        e.preventDefault();
        return;
    }
    const missesBtn = e.target.closest('[data-retry-misses]');
    if (missesBtn) {
        missesBtn.disabled = true;
        fetch('/api/retry-misses/' + missesBtn.dataset.retryMisses, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrfToken() },
        })
            .then((r) => r.json())
            .then((data) => {
                if (!data.ok) {
                    if (data.message) toast(data.message, 'danger');
                } else {
                    toast('Queued ' + data.queued + ' newly matched track(s).' +
                        (data.remaining ? ' ' + data.remaining + ' still unmatched.' : ''), 'success');
                }
                missesBtn.disabled = false;
                if (typeof refreshTable === 'function') refreshTable();
            })
            .catch(() => {
                toast('Could not retry unmatched tracks.', 'danger');
                missesBtn.disabled = false;
            });
        e.preventDefault();
        return;
    }
    const revealBtn = e.target.closest('[data-reveal]');
    if (revealBtn) {
        revealBtn.disabled = true;
        fetch('/api/reveal/' + revealBtn.dataset.reveal)
            .then((r) => r.json())
            .then((data) => {
                if (!data.ok && data.message) toast(data.message, 'danger');
            })
            .catch(() => toast('Could not open your file manager.', 'danger'))
            .finally(() => { revealBtn.disabled = false; });
        e.preventDefault();
    }
});

// --- Playlists: expandable per-track rows --------------------------------

function shortUrl(url) {
    return url.length > 40 ? url.slice(0, 40) + '...' : url;
}

// Builds a <tr> matching the server-rendered layout. Used to lazily list a
// playlist's tracks when its parent row is expanded.
function historyDayLabel(iso) {
    // 'YYYY-MM-DD …' -> Today / Yesterday / weekday label, matching server.
    const m = /^\s*(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
    if (!m) return 'Unknown date';
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const today = now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate());
    const stamp = m[1] + '-' + m[2] + '-' + m[3];
    if (stamp === today) return 'Today';
    const y = new Date(now.getTime() - 86400000);
    const yesterday = y.getFullYear() + '-' + pad(y.getMonth() + 1) + '-' + pad(y.getDate());
    if (stamp === yesterday) return 'Yesterday';
    const days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    return days[d.getDay()] + ', ' + months[d.getMonth()] + ' ' + m[3];
}

function renderHistoryRow(item) {
    const tr = document.createElement('tr');
    tr.id = 'row-' + item.id;
    tr.dataset.day = historyDayLabel(item.created_at);
    if (item.parent_id) tr.dataset.playlistId = item.parent_id;
    if (item.output_path) tr.dataset.folder = item.output_path;

    const statusTd = document.createElement('td');
    statusTd.className = 'row-status';
    const select = document.createElement('input');
    select.type = 'checkbox';
    select.className = 'form-check-input row-select me-1';
    select.title = 'Select row';
    select.setAttribute('aria-label', 'Select row');
    select.setAttribute('data-bulk', item.id);
    statusTd.appendChild(select);
    const badge = document.createElement('span');
    badge.className = 'badge status-badge status-' + item.status;
    badge.textContent = item.status;
    statusTd.appendChild(badge);
    const progressWrap = document.createElement('div');
    progressWrap.className = 'progress mt-1 row-progress';
    progressWrap.style.height = '4px';
    const progressBar = document.createElement('div');
    progressBar.className = 'progress-bar row-progress-bar bg-primary';
    progressBar.style.width = item.progress + '%';
    progressWrap.appendChild(progressBar);
    statusTd.appendChild(progressWrap);
    const speedLine = document.createElement('div');
    speedLine.className = 'row-speed text-muted small';
    speedLine.textContent = speedText(item);
    statusTd.appendChild(speedLine);
    tr.appendChild(statusTd);

    const fmtTd = document.createElement('td');
    fmtTd.textContent = item.format;
    tr.appendChild(fmtTd);

    const urlTd = document.createElement('td');
    const link = document.createElement('a');
    link.href = item.url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = shortUrl(item.url);
    urlTd.appendChild(link);
    tr.appendChild(urlTd);

    const fileTd = document.createElement('td');
    fileTd.className = 'row-file';
    if (item.status === 'completed' || item.status === 'skipped') {
        renderSavedFileCell(fileTd, item);
    } else if (item.status === 'failed') {
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn btn-sm btn-outline-info retry-btn';
        retry.title = 'Try this download again';
        retry.setAttribute('data-retry', item.id);
        retry.textContent = 'Retry';
        fileTd.appendChild(retry);
    } else if (['pending', 'downloading', 'converting'].includes(item.status)) {
        if (item.status === 'pending') {
            const top = document.createElement('button');
            top.type = 'button';
            top.className = 'btn btn-sm btn-outline-secondary top-btn me-1';
            top.title = 'Move to the top of the queue';
            top.setAttribute('data-top', item.id);
            top.textContent = 'Top';
            fileTd.appendChild(top);
        }
        const pause = document.createElement('button');
        pause.type = 'button';
        pause.className = 'btn btn-sm btn-outline-info pause-btn me-1';
        pause.title = 'Pause this download (resumable)';
        pause.setAttribute('data-pause', item.id);
        pause.textContent = 'Pause';
        fileTd.appendChild(pause);
        const skip = document.createElement('button');
        skip.type = 'button';
        skip.className = 'btn btn-sm btn-outline-warning skip-btn';
        skip.title = 'Skip this track';
        skip.setAttribute('data-skip', item.id);
        skip.textContent = 'Skip';
        fileTd.appendChild(skip);
    } else if (item.status === 'paused') {
        const resume = document.createElement('button');
        resume.type = 'button';
        resume.className = 'btn btn-sm btn-outline-success resume-btn me-1';
        resume.title = 'Resume this download';
        resume.setAttribute('data-resume', item.id);
        resume.textContent = 'Resume';
        fileTd.appendChild(resume);
        const skip = document.createElement('button');
        skip.type = 'button';
        skip.className = 'btn btn-sm btn-outline-warning skip-btn';
        skip.title = 'Skip this track';
        skip.setAttribute('data-skip', item.id);
        skip.textContent = 'Skip';
        fileTd.appendChild(skip);
    } else {
        fileTd.innerHTML = '<span class="text-muted small">&mdash;</span>';
    }
    tr.appendChild(fileTd);

    const dateTd = document.createElement('td');
    dateTd.textContent = String(item.created_at || '').slice(0, 16) || 'N/A';
    tr.appendChild(dateTd);

    return tr;
}

function speedText(item) {
    if (!['downloading', 'converting'].includes(item.status)) return '';
    const bits = [];
    if (item.dl_speed) bits.push(item.dl_speed);
    if (item.dl_eta) bits.push('ETA ' + item.dl_eta);
    return bits.join(' \u00b7 ');
}

function fmtRuntime(totalSeconds) {
    const total = Math.round(totalSeconds || 0);
    if (total <= 0) return '';
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    return h ? h + 'h ' + m + 'm total' : m + 'm total';
}

function togglePlaylist(btn) {
    const row = document.getElementById('children-' + btn.dataset.expandPlaylist);
    if (!row) return;
    const box = row.querySelector('.playlist-children-box');
    const caret = btn.querySelector('.expand-caret');

    if (row.hidden) {
        row.hidden = false;
        if (caret) caret.textContent = '\u25bc';
        if (!box.dataset.loaded) {
            box.innerHTML = '<div class="small text-muted py-2">Loading tracks\u2026</div>';
            fetch('/api/playlist/' + btn.dataset.expandPlaylist)
                .then((r) => r.json())
                .then((data) => {
                    box.dataset.loaded = '1';
                    box.innerHTML = '';
                    (data.items || []).forEach((item) => box.appendChild(renderHistoryRow(item)));
                    const total = (data.items || []).reduce(
                        (sum, item) => sum + (Number(item.duration) || 0), 0);
                    const label = fmtRuntime(total);
                    if (label) {
                        const parentRow = document.getElementById('row-' + btn.dataset.expandPlaylist);
                        const summary = parentRow ? parentRow.querySelector('[data-playlist-summary]') : null;
                        if (summary) {
                            summary.dataset.runtime = ' · ' + label;
                            summary.textContent += ' · ' + label;
                        }
                    }
                })
                .catch(() => {
                    box.innerHTML = '<div class="small text-danger py-2">Could not load tracks.</div>';
                });
        }
    } else {
        row.hidden = true;
        if (caret) caret.textContent = '\u25b6';
    }
}

// --- Server search: finds tracks inside unexpanded playlists -------------

let searchTimer = null;

function initServerSearch() {
    const input = document.getElementById('historySearch');
    const box = document.getElementById('searchResults');
    if (!input || !box) return;
    input.addEventListener('input', () => {
        clearTimeout(searchTimer);
        const q = input.value.trim();
        if (q.length < 2) {
            box.innerHTML = '';
            box.classList.add('d-none');
            return;
        }
        searchTimer = setTimeout(() => runServerSearch(q), 300);
    });
    document.addEventListener('click', (e) => {
        if (e.target !== input && !(e.target.closest && e.target.closest('#searchResults'))) {
            box.classList.add('d-none');
        }
    });
}

function runServerSearch(q) {
    const box = document.getElementById('searchResults');
    if (!box) return;
    fetch('/api/search?q=' + encodeURIComponent(q))
        .then((r) => r.json())
        .then((data) => {
            box.innerHTML = '';
            const items = (data.items || []).slice(0, 8);
            if (!items.length) {
                box.classList.add('d-none');
                return;
            }
            items.forEach((item) => {
                const li = document.createElement('li');
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'search-result-item';
                const label = item.filename || item.playlist_title || item.url;
                btn.textContent = label + ' (' + item.status + ')' +
                    (item.parent_id ? ' · in playlist' : '');
                btn.title = item.output_path || item.url;
                btn.addEventListener('click', () => {
                    box.classList.add('d-none');
                    jumpToRow(item);
                });
                li.appendChild(btn);
                box.appendChild(li);
            });
            box.classList.remove('d-none');
        })
        .catch(() => {});
}

function highlightRow(row) {
    row.classList.add('search-hit');
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setTimeout(() => row.classList.remove('search-hit'), 4000);
}

function jumpToRow(item) {
    // Reset client-side filters/paging so the target can be shown.
    historyState.q = '';
    historyState.status = 'all';
    historyState.folder = 'all';
    historyState.page = 0;
    const search = document.getElementById('historySearch');
    if (search) search.value = '';
    const status = document.getElementById('historyStatus');
    if (status) status.value = 'all';
    const folder = document.getElementById('historyFolder');
    if (folder) folder.value = 'all';

    const reveal = () => {
        for (let page = 0; page < 50; page++) {
            historyState.page = page;
            applyHistoryFilter();
            const row = document.getElementById('row-' + item.id);
            if (row && !row.hidden) {
                highlightRow(row);
                return true;
            }
        }
        historyState.page = 0;
        applyHistoryFilter();
        return false;
    };

    if (item.parent_id) {
        const expandBtn = document.querySelector(
            '[data-expand-playlist="' + item.parent_id + '"]');
        const childRow = document.getElementById('children-' + item.parent_id);
        if (expandBtn && childRow && childRow.hidden) {
            expandBtn.click();
        }
        let tries = 0;
        const waiter = setInterval(() => {
            tries += 1;
            if (document.getElementById('row-' + item.id) || tries > 25) {
                clearInterval(waiter);
                if (!reveal()) toast('Found in the database, but the row is not on this page.', 'info');
            }
        }, 200);
    } else if (!reveal()) {
        toast('Found in the database, but the row is not on this page.', 'info');
    }
}

// --- Subscriptions: follow playlists for new tracks ------------------------

function postJSON(url, payload) {
    return fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
        body: JSON.stringify(payload || {}),
    }).then((r) => r.json());
}

function loadSubscriptions() {
    const list = document.getElementById('subsList');
    if (!list) return;
    fetch('/api/subscriptions')
        .then((r) => r.json())
        .then((data) => {
            const items = data.items || [];
            list.innerHTML = '';
            if (!items.length) {
                list.innerHTML = '<span class="text-muted small">No followed playlists yet — hit Follow on any finished playlist.</span>';
                return;
            }
            items.forEach((sub) => {
                const row = document.createElement('div');
                row.className = 'd-flex flex-wrap gap-2 align-items-center mb-2 sub-row';
                const name = document.createElement('strong');
                name.textContent = sub.playlist_title || sub.url;
                name.title = sub.url;
                row.appendChild(name);
                const meta = document.createElement('span');
                meta.className = 'text-muted small';
                meta.textContent = (sub.active ? 'Active' : 'Paused') +
                    ' · every ' + sub.interval_hours + 'h' +
                    (sub.last_checked ? ' · checked ' + sub.last_checked.slice(0, 16) : '');
                row.appendChild(meta);
                const freq = document.createElement('select');
                freq.className = 'form-select form-select-sm';
                freq.style.width = 'auto';
                freq.title = 'Check frequency';
                [6, 12, 24, 168].forEach((hours) => {
                    const opt = document.createElement('option');
                    opt.value = String(hours);
                    opt.textContent = hours >= 168 ? 'Weekly' : 'Every ' + hours + 'h';
                    if (hours === sub.interval_hours) opt.selected = true;
                    freq.appendChild(opt);
                });
                freq.addEventListener('change', () => {
                    postJSON('/api/subscriptions/' + sub.id + '/interval',
                             { interval_hours: Number(freq.value) })
                        .then(() => loadSubscriptions())
                        .catch(() => toast('Could not change frequency.', 'danger'));
                });
                row.appendChild(freq);
                const mkBtn = (label, title, fn) => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'btn btn-sm btn-outline-secondary';
                    btn.textContent = label;
                    btn.title = title;
                    btn.addEventListener('click', fn);
                    row.appendChild(btn);
                    return btn;
                };
                mkBtn('Check now', 'Look for new tracks right now', () => {
                    postJSON('/api/subscriptions/' + sub.id + '/check', {})
                        .then((res) => {
                            toast(res.ok ? 'Found ' + res.added + ' new track(s).' : (res.error || 'Check failed.'),
                                  res.ok ? 'success' : 'danger');
                            loadSubscriptions();
                            if (typeof refreshTable === 'function') refreshTable();
                        })
                        .catch(() => toast('Could not check this playlist.', 'danger'));
                });
                mkBtn(sub.active ? 'Pause' : 'Resume', 'Pause or resume automatic checks', () => {
                    postJSON('/api/subscriptions/' + sub.id + '/toggle', {})
                        .then(loadSubscriptions)
                        .catch(() => toast('Could not change this subscription.', 'danger'));
                });
                mkBtn('Unfollow', 'Stop checking (history is kept)', () => {
                    if (!confirm('Stop following this playlist? Your downloaded tracks stay.')) return;
                    postJSON('/api/subscriptions/' + sub.id + '/delete', {})
                        .then(loadSubscriptions)
                        .catch(() => toast('Could not remove this subscription.', 'danger'));
                });
                const filters = document.createElement('details');
                filters.className = 'sub-filters small';
                const summary = document.createElement('summary');
                const activeFilters = (sub.skip_shorts ? 1 : 0) +
                    (sub.min_duration > 0 ? 1 : 0) +
                    (sub.title_include ? 1 : 0) + (sub.title_exclude ? 1 : 0);
                summary.textContent = activeFilters
                    ? 'Filters (' + activeFilters + ' on)' : 'Filters';
                summary.title = 'Only queue matching new tracks';
                filters.appendChild(summary);
                const grid = document.createElement('div');
                grid.className = 'd-flex flex-wrap gap-2 align-items-center mt-1';
                const shortsLabel = document.createElement('label');
                shortsLabel.className = 'd-flex gap-1 align-items-center';
                const shorts = document.createElement('input');
                shorts.type = 'checkbox';
                shorts.checked = !!sub.skip_shorts;
                shorts.title = 'Skip clips of a minute or less';
                shortsLabel.appendChild(shorts);
                shortsLabel.appendChild(document.createTextNode('Skip shorts'));
                grid.appendChild(shortsLabel);
                const minDur = document.createElement('input');
                minDur.type = 'number';
                minDur.min = '0';
                minDur.max = '36000';
                minDur.value = sub.min_duration || 0;
                minDur.title = 'Minimum seconds (0 = off)';
                minDur.className = 'form-control form-control-sm';
                minDur.style.width = '7rem';
                minDur.placeholder = 'Min secs';
                grid.appendChild(minDur);
                const inc = document.createElement('input');
                inc.type = 'text';
                inc.value = sub.title_include || '';
                inc.placeholder = 'Title must contain…';
                inc.title = 'Comma-separated; matches if any term hits';
                inc.className = 'form-control form-control-sm';
                inc.style.width = '11rem';
                grid.appendChild(inc);
                const exc = document.createElement('input');
                exc.type = 'text';
                exc.value = sub.title_exclude || '';
                exc.placeholder = 'Title must not contain…';
                exc.title = 'Comma-separated; drops on any hit';
                exc.className = 'form-control form-control-sm';
                exc.style.width = '11rem';
                grid.appendChild(exc);
                const save = document.createElement('button');
                save.type = 'button';
                save.className = 'btn btn-sm btn-outline-primary';
                save.textContent = 'Save filters';
                save.addEventListener('click', () => {
                    postJSON('/api/subscriptions/' + sub.id + '/filters', {
                        min_duration: Number(minDur.value) || 0,
                        skip_shorts: shorts.checked,
                        title_include: inc.value,
                        title_exclude: exc.value,
                    })
                        .then((res) => {
                            toast(res.ok ? 'Filters saved.' : (res.message || 'Could not save.'),
                                  res.ok ? 'success' : 'danger');
                            loadSubscriptions();
                        })
                        .catch(() => toast('Could not save filters.', 'danger'));
                });
                grid.appendChild(save);
                filters.appendChild(grid);
                list.appendChild(row);
                list.appendChild(filters);
            });
        })
        .catch(() => {});
}

document.addEventListener('DOMContentLoaded', loadSubscriptions);

// --- Health banner: stale downloader suspicion -----------------------------

function initHealthBanner() {
    const banner = document.getElementById('healthBanner');
    if (!banner) return;
    fetch('/api/health')
        .then((r) => r.json())
        .then((data) => {
            const problems = [];
            if (data.stale_helper_suspected) {
                problems.push('downloads keep failing — this looks like an outdated downloader (update yt-dlp in Settings › Helpers)');
            }
            if (data.app_update && data.app_update.version) {
                problems.push('Audio Converter ' + data.app_update.version + ' is available (Settings › Check for updates)');
            }
            if (data.resumed > 0) {
                let seenResume = null;
                try {
                    seenResume = sessionStorage.getItem('resumeNoticed');
                } catch (err) { /* private mode */ }
                if (!seenResume) {
                    try {
                        sessionStorage.setItem('resumeNoticed', '1');
                    } catch (err) { /* private mode */ }
                    problems.push('Resumed ' + data.resumed + ' interrupted download(s) from last time.');
                }
            }
            if (data.ffmpeg === false) {
                problems.push('FFmpeg was not found — conversions cannot run until it is installed');
            }
            if (data.ytdlp === false) {
                problems.push('yt-dlp was not found — downloads cannot start until it is installed');
            }
            if (data.output_writable === false) {
                problems.push('the output folder is not writable');
            }
            if (data.secret_persisted === false) {
                problems.push('session secret is not persisted — logins and CSRF tokens reset on every relaunch (set AUDIO_CONVERTER_SECRET_KEY or check write access to the data directory)');
            }
            if (data.signature && data.signature.signed === false) {
                problems.push('this install is NOT code-signed — Gatekeeper will warn on first launch (open with right-click › Open once). Build with a Developer ID to silence this.');
            }
            if (data.signature && data.signature.signed === true && data.signature.notarized === false) {
                problems.push('this install is signed but not notarized — first launch requires right-click › Open.');
            }
            if (!problems.length) return;
            const text = document.getElementById('healthBannerText');
            if (text) text.textContent = problems.join(' Also: ') + '.';
            banner.classList.remove('d-none');
        })
        .catch(() => {});

    // Migration banner: shows once after the database is upgraded by this
    // build. Dismissable; the dismissed state lives in user_settings.
    fetch('/api/migration-info')
        .then((r) => r.json())
        .then((m) => {
            if (!m || !m.upgrade) return;
            const banner = document.getElementById('migrationBanner');
            const text = document.getElementById('migrationBannerText');
            if (!banner || !text) return;
            text.textContent = m.message;
            banner.classList.remove('d-none');
            document.addEventListener('click', (ev) => {
                if (ev.target.closest && ev.target.closest('[data-hide-migration]')) {
                    fetch('/api/migration-ack', {
                        method: 'POST',
                        headers: { 'X-CSRFToken': csrfToken() },
                    }).catch(() => {});
                    banner.classList.add('d-none');
                }
            });
        })
        .catch(() => {});

    // "What's new" — surfaces after a packaged upgrade until the user
    // opens /changelog (which records the running version as seen).
    fetch('/api/whats-new')
        .then((r) => r.json())
        .then((w) => {
            if (!w || !w.is_new) return;
            const banner = document.getElementById('whatsNewBanner');
            if (!banner) return;
            const text = banner.querySelector('[data-whats-new-text]');
            if (text) text.textContent =
                'Audio Converter ' + (w.version || '') + ' is here — see what changed.';
            banner.classList.remove('d-none');
        })
        .catch(() => {});
    document.addEventListener('click', (e) => {
        if (e.target.closest && e.target.closest('[data-hide-health]')) {
            banner.classList.add('d-none');
        }
    });
}

document.addEventListener('DOMContentLoaded', initHealthBanner);

// --- Library stats ---------------------------------------------------------

function readableBytes(num) {
    const value = Number(num) || 0;
    if (value < 1024) return value + ' B';
    const units = ['KB', 'MB', 'GB', 'TB'];
    let v = value / 1024;
    let u = 0;
    while (v >= 1024 && u < units.length - 1) {
        v /= 1024;
        u += 1;
    }
    return v.toFixed(1) + ' ' + units[u];
}

function loadStats() {
    const body = document.getElementById('statsBody');
    if (!body) return;
    const esc = (s) => String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    fetch('/api/stats')
        .then((r) => r.json())
        .then((data) => {
            if (!data.ok) {
                body.innerHTML = '<span class="text-muted small">Stats unavailable.</span>';
                return;
            }
            const tile = (label, value) =>
                '<div class="col-6 col-md-3 mb-2"><div class="stat-tile">' +
                '<div class="stat-value">' + esc(value) + '</div>' +
                '<div class="stat-label">' + esc(label) + '</div></div></div>';
            let html = '<div class="row">';
            html += tile('Tracks', data.tracks);
            html += '<div class="col-6 col-md-3 mb-2"><div class="stat-tile">' +
                '<div class="stat-value">' + esc(data.playtime) + '</div>' +
                '<div class="stat-label">Playtime <span class="stat-sub">across ' +
                data.playtime_tracks + ' tracks</span></div></div></div>';
            html += tile('Files', data.files);
            html += tile('New this week', data.recent_7d);
            html += '</div>';
            const formats = Object.keys(data.by_format || {}).sort().map(
                (f) => esc(f) + ' (' + data.by_format[f] + ')').join(' · ');
            if (formats) html += '<div class="small text-muted">Formats: ' + formats + '</div>';
            const tops = data.top_playlists || [];
            if (tops.length) {
                html += '<div class="small text-muted mt-1">Biggest playlists: ' +
                    tops.map((p) => esc(p.title) + ' (' + p.tracks + ')').join(' · ') + '</div>';
            }
            const folders = data.top_folders || [];
            if (folders.length) {
                html += '<div class="small text-muted mt-1">Largest folders: ' +
                    folders.map((f) => esc(f.folder) + ' (' + readableBytes(f.bytes) + ')').join(' · ') + '</div>';
            }
            body.innerHTML = html;
        })
        .catch(() => {
            body.innerHTML = '<span class="text-muted small">Stats unavailable.</span>';
        });
}

document.addEventListener('DOMContentLoaded', loadStats);

// --- Bulk selection: retry or delete many rows at once --------------------

function selectedRows() {
    return Array.from(document.querySelectorAll('#historyBody .row-select'))
        .filter((box) => box.checked)
        .map((box) => {
            const tr = box.closest('tr');
            const badge = tr ? tr.querySelector('.status-badge') : null;
            return {
                id: box.dataset.bulk,
                status: badge ? badge.textContent.trim() : '',
                isPlaylist: tr ? tr.classList.contains('playlist-row') : false,
                row: tr,
            };
        })
        .filter((item) => item.id && item.row);
}

function refreshBulkBar() {
    const bar = document.getElementById('bulkBar');
    const count = document.getElementById('bulkCount');
    const all = document.getElementById('selectAll');
    if (!bar) return;
    const items = selectedRows();
    bar.classList.toggle('on', items.length > 0);
    if (count) count.textContent = items.length ? items.length + ' selected' : '';
    if (all) {
        const visible = Array.from(
            document.querySelectorAll('#historyBody > tr:not(.playlist-children)'))
            .filter((tr) => !tr.hidden)
            .map((tr) => tr.querySelector('.row-select'))
            .filter(Boolean);
        all.checked = visible.length > 0 && visible.every((box) => box.checked);
    }
}

function postOne(url) {
    return fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken() },
    }).then((r) => r.json().catch(() => ({ ok: false })));
}

function initBulkActions() {
    const body = document.getElementById('historyBody');
    if (!body) return;
    const all = document.getElementById('selectAll');
    if (all) {
        all.addEventListener('change', () => {
            Array.from(body.querySelectorAll(':scope > tr:not(.playlist-children)'))
                .filter((tr) => !tr.hidden)
                .forEach((tr) => {
                    const box = tr.querySelector('.row-select');
                    if (box) box.checked = all.checked;
                });
            refreshBulkBar();
        });
    }
    body.addEventListener('change', (e) => {
        if (e.target.closest && e.target.closest('.row-select')) refreshBulkBar();
    });
    const clear = document.getElementById('bulkClear');
    if (clear) {
        clear.addEventListener('click', () => {
            Array.from(body.querySelectorAll('.row-select')).forEach((box) => { box.checked = false; });
            refreshBulkBar();
        });
    }
    const retry = document.getElementById('bulkRetry');
    if (retry) {
        retry.addEventListener('click', () => {
            const items = selectedRows().filter((item) => item.status === 'failed');
            if (!items.length) {
                toast('Nothing failed among the selection.', 'info');
                return;
            }
            retry.disabled = true;
            let done = 0;
            let okCount = 0;
            const step = () => {
                if (done >= items.length) {
                    retry.disabled = false;
                    retry.textContent = 'Retry';
                    toast('Re-queued ' + okCount + ' of ' + items.length + ' failed track(s).',
                          okCount ? 'success' : 'danger');
                    if (typeof refreshTable === 'function') refreshTable();
                    return;
                }
                const item = items[done];
                done += 1;
                retry.textContent = 'Retry (' + done + '/' + items.length + ')';
                postOne('/api/retry/' + item.id).then((data) => {
                    if (data.ok) okCount += 1;
                    step();
                }).catch(step);
            };
            step();
        });
    }
    const transcode = document.getElementById('bulkTranscode');
    if (transcode) {
        transcode.addEventListener('click', () => {
            const items = selectedRows().filter((item) =>
                (item.status === 'completed' || item.status === 'skipped') && !item.isPlaylist);
            if (!items.length) {
                toast('Nothing convertible among the selection.', 'info');
                return;
            }
            const fmtSel = document.getElementById('bulkFormat');
            const format = fmtSel ? fmtSel.value : 'flac';
            if (!confirm('Convert ' + items.length + ' track(s) to ' +
                         (fmtSel ? fmtSel.options[fmtSel.selectedIndex].text : format) +
                         ' without re-downloading?')) {
                return;
            }
            transcode.disabled = true;
            fetch('/api/transcode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
                body: JSON.stringify({ ids: items.map((i) => i.id), format: format }),
            })
                .then((r) => r.json())
                .then((data) => {
                    transcode.disabled = false;
                    if (!data.ok) {
                        toast(data.message || 'Could not transcode.', 'danger');
                        return;
                    }
                    toast('Converted ' + data.converted + ' of ' + items.length + ' track(s).',
                          data.converted ? 'success' : 'danger');
                    if (typeof refreshTable === 'function') refreshTable();
                })
                .catch(() => {
                    transcode.disabled = false;
                    toast('Could not transcode.', 'danger');
                });
        });
    }
    const queue = document.getElementById('bulkQueue');
    if (queue && window.AudioPlayer) {
        queue.addEventListener('click', () => {
            const items = selectedRows().filter((item) =>
                (item.status === 'completed' || item.status === 'skipped') && !item.isPlaylist);
            if (!items.length) {
                toast('Nothing playable among the selection.', 'info');
                return;
            }
            if (!window.AudioPlayer || !window.AudioPlayer.enqueue) {
                toast('Player is not ready.', 'danger');
                return;
            }
            queue.disabled = true;
            let done = 0;
            let okCount = 0;
            const step = () => {
                if (done >= items.length) {
                    queue.disabled = false;
                    queue.textContent = 'Queue';
                    toast('Added ' + okCount + ' of ' + items.length + ' track(s) to the player queue.',
                          okCount ? 'success' : 'danger');
                    return;
                }
                const item = items[done];
                done += 1;
                queue.textContent = 'Queue (' + done + '/' + items.length + ')';
                fetch('/api/track/' + item.id)
                    .then((r) => r.json())
                    .then((data) => {
                        if (data.ok) {
                            window.AudioPlayer.enqueue(data.item);
                            okCount += 1;
                        }
                        step();
                    })
                    .catch(step);
            };
            step();
        });
    }
    const del = document.getElementById('bulkDelete');
    if (del) {
        del.addEventListener('click', () => {
            const items = selectedRows();
            if (!items.length) return;
            if (!confirm('Delete ' + items.length + ' selected item(s) from disk and history?')) return;
            del.disabled = true;
            let done = 0;
            let okCount = 0;
            const step = () => {
                if (done >= items.length) {
                    del.disabled = false;
                    del.textContent = 'Delete';
                    toast('Deleted ' + okCount + ' of ' + items.length + ' selected item(s).',
                          okCount ? 'success' : 'danger');
                    refreshBulkBar();
                    refreshStorageInfo();
                    if (typeof refreshTable === 'function') refreshTable();
                    return;
                }
                const item = items[done];
                done += 1;
                del.textContent = 'Delete (' + done + '/' + items.length + ')';
                const url = item.isPlaylist ? '/api/delete-playlist/' + item.id : '/api/delete/' + item.id;
                postOne(url).then((data) => {
                    if (data.ok) {
                        okCount += 1;
                        if (item.row) item.row.remove();
                    }
                    step();
                }).catch(step);
            };
            step();
        });
    }
}

document.addEventListener('DOMContentLoaded', initBulkActions);

document.addEventListener('click', (e) => {
    const expandBtn = e.target.closest('[data-expand-playlist]');
    if (expandBtn) {
        togglePlaylist(expandBtn);
        e.preventDefault();
        return;
    }
});

// --- History table: search / status filter / pagination --------------------
// Client-side only; rows are server-rendered. Playlist children rows always
// travel with their parent row (and stay collapsed unless expanded).

const historyState = { q: '', status: 'all', folder: 'all', sort: 'newest', page: 0, perPage: 15 };

// Signature of the last applied filter state. Live badge/progress updates
// bypass applyHistoryFilter (they patch rows in place), so when nothing
// structural changed we skip all DOM writes to avoid layout churn.
let historyLastSig = null;

function historyTopRows() {
    const body = document.getElementById('historyBody');
    if (!body) return [];
    return Array.from(body.children).filter(
        (tr) => tr.tagName === 'TR' && !tr.classList.contains('playlist-children') &&
            !tr.classList.contains('history-day'));
}

function historyRowStatus(tr) {
    const badge = tr.querySelector('.status-badge');
    return badge ? badge.textContent.trim().toLowerCase() : '';
}

function historyRowMatches(tr) {
    if (historyState.status !== 'all' && historyRowStatus(tr) !== historyState.status) {
        return false;
    }
    if (historyState.folder !== 'all') {
        const folder = tr.dataset.folder || '';
        if (folder !== historyState.folder &&
            folder.indexOf(historyState.folder + '/') !== 0) {
            return false;
        }
    }
    if (historyState.q && tr.textContent.toLowerCase().indexOf(historyState.q) === -1) {
        return false;
    }
    return true;
}

function historyIsExpanded(parentId) {
    const caret = document.querySelector(
        '[data-expand-playlist="' + parentId + '"] .expand-caret');
    return caret ? caret.textContent === '▼' : false; // '\u25bc' expanded
}

function historyRowDate(tr) {
    const cells = tr.querySelectorAll('td');
    const last = cells[cells.length - 1];
    return last ? last.textContent.trim() : '';
}

function historyRowName(tr) {
    const name = tr.querySelector('.file-name');
    if (name && name.textContent.trim()) return name.textContent.trim().toLowerCase();
    const link = tr.querySelector('td a');
    return link ? link.textContent.trim().toLowerCase() : '';
}

function historyChildRow(tr) {
    const btn = tr.querySelector('[data-expand-playlist]');
    if (!btn) return null;
    return document.getElementById('children-' + btn.dataset.expandPlaylist);
}

function applyHistoryFilter() {
    const body = document.getElementById('historyBody');
    if (!body) return;
    const rows = historyTopRows();
    const ordered = rows.slice();
    if (historyState.sort === 'oldest') {
        ordered.sort((a, b) => (historyRowDate(a) < historyRowDate(b) ? -1 : 1));
    } else if (historyState.sort === 'name') {
        ordered.sort((a, b) => historyRowName(a).localeCompare(historyRowName(b)));
    }
    ordered.forEach((tr) => {
        body.appendChild(tr);
        const child = historyChildRow(tr);
        if (child) body.appendChild(child);
    });
    const visible = ordered.filter(historyRowMatches);
    const sig = [
        historyState.sort, historyState.q, historyState.status,
        historyState.folder, historyState.page,
        visible.map((tr) => tr.id).join(','),
    ].join('|');
    if (sig === historyLastSig) return;
    historyLastSig = sig;
    const pages = Math.max(1, Math.ceil(visible.length / historyState.perPage));
    if (historyState.page >= pages) historyState.page = pages - 1;
    if (historyState.page < 0) historyState.page = 0;
    const start = historyState.page * historyState.perPage;
    const pageSet = new Set(visible.slice(start, start + historyState.perPage));
    rows.forEach((tr) => { tr.hidden = !pageSet.has(tr); });
    Array.from(body.querySelectorAll(':scope > tr.playlist-children')).forEach((child) => {
        const m = (child.id || '').match(/^children-(\d+)$/);
        const parent = m ? document.getElementById('row-' + m[1]) : null;
        child.hidden = !parent || parent.hidden || !historyIsExpanded(m ? m[1] : '');
    });
    const count = document.getElementById('historyCount');
    if (count) {
        if (!visible.length) {
            count.textContent = 'No conversions match';
        } else {
            count.textContent = 'Showing ' + (start + 1) + '–' +
                Math.min(start + historyState.perPage, visible.length) +
                ' of ' + visible.length;
        }
    }
    const prev = document.getElementById('historyPrev');
    const next = document.getElementById('historyNext');
    if (prev) prev.disabled = historyState.page <= 0;
    if (next) next.disabled = historyState.page >= pages - 1;
    // Day headers are rebuilt from the rows in their current (possibly
    // sorted/paged) order, so headers never detach from their rows.
    Array.from(body.querySelectorAll(':scope > tr.history-day')).forEach((h) => h.remove());
    // Show headers only in chronological sorts; name sort mixes dates.
    const chronological = historyState.sort !== 'name';
    if (chronological) {
        let lastDay = null;
        ordered.forEach((tr) => {
            if (!pageSet.has(tr)) return;
            const day = tr.dataset.day || 'Unknown date';
            if (day !== lastDay) {
                lastDay = day;
                const header = document.createElement('tr');
                header.className = 'history-day';
                const td = document.createElement('td');
                td.colSpan = 5;
                td.textContent = day;
                header.appendChild(td);
                body.insertBefore(header, tr);
            }
        });
    }
}

function historyFolderLabel(path) {
    const parts = String(path || '').split(/[\\/]/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : String(path || '');
}

function historyRowDir(folder) {
    // File rows point at a file; group them by their directory.
    if (/\.(flac|m4a|wav|ogg)$/i.test(folder || '')) {
        return folder.split(/[\\/]/).slice(0, -1).join('/');
    }
    return folder || '';
}

function buildHistoryFolderOptions() {
    const select = document.getElementById('historyFolder');
    if (!select) return;
    const seen = new Map();
    historyTopRows().forEach((tr) => {
        const dir = historyRowDir(tr.dataset.folder);
        if (dir && !seen.has(dir)) seen.set(dir, historyFolderLabel(dir));
    });
    const current = select.value || 'all';
    select.innerHTML = '';
    const all = document.createElement('option');
    all.value = 'all';
    all.textContent = 'All folders';
    select.appendChild(all);
    Array.from(seen.entries())
        .sort((a, b) => a[1].localeCompare(b[1]))
        .forEach((pair) => {
            const value = pair[0];
            const label = pair[1];
            const opt = document.createElement('option');
            opt.value = value;
            opt.textContent = label;
            opt.title = value;
            select.appendChild(opt);
        });
    select.value = seen.has(current) ? current : 'all';
    historyState.folder = select.value;
}

function refreshStorageInfo() {
    const el = document.getElementById('storageInfo');
    if (!el) return;
    fetch('/api/storage')
        .then((r) => r.json())
        .then((data) => {
            if (data.ok) {
                el.textContent = 'Library: ' + data.readable + ' · ' + data.files + ' files';
            }
        })
        .catch(() => {});
}

function refreshQueueButton() {
    const btn = document.getElementById('queuePause');
    if (!btn) return;
    fetch('/api/queue/status')
        .then((r) => r.json())
        .then((data) => {
            btn.textContent = data.paused ? 'Resume all' : 'Pause all';
            btn.dataset.paused = data.paused ? '1' : '';
        })
        .catch(() => {});
}

function initHistoryToolbar() {
    if (!document.getElementById('historyBody')) return;
    const search = document.getElementById('historySearch');
    const status = document.getElementById('historyStatus');
    const prev = document.getElementById('historyPrev');
    const next = document.getElementById('historyNext');
    if (search) {
        search.addEventListener('input', () => {
            historyState.q = search.value.trim().toLowerCase();
            historyState.page = 0;
            applyHistoryFilter();
        });
    }
    if (status) {
        status.addEventListener('change', () => {
            historyState.status = status.value;
            historyState.page = 0;
            applyHistoryFilter();
        });
    }
    buildHistoryFolderOptions();
    const folder = document.getElementById('historyFolder');
    if (folder) {
        folder.addEventListener('change', () => {
            historyState.folder = folder.value;
            historyState.page = 0;
            applyHistoryFilter();
        });
    }
    if (prev) {
        prev.addEventListener('click', () => {
            if (historyState.page > 0) {
                historyState.page -= 1;
                applyHistoryFilter();
            }
        });
    }
    if (next) {
        next.addEventListener('click', () => {
            historyState.page += 1;
            applyHistoryFilter();
        });
    }
    const sortSel = document.getElementById('historySort');
    if (sortSel) {
        sortSel.addEventListener('change', () => {
            historyState.sort = sortSel.value;
            historyState.page = 0;
            applyHistoryFilter();
        });
    }
    const queueBtn = document.getElementById('queuePause');
    if (queueBtn) {
        queueBtn.addEventListener('click', () => {
            const paused = queueBtn.dataset.paused === '1';
            fetch(paused ? '/api/queue/resume' : '/api/queue/pause')
                .then((r) => r.json())
                .then((data) => {
                    queueBtn.textContent = data.paused ? 'Resume all' : 'Pause all';
                    queueBtn.dataset.paused = data.paused ? '1' : '';
                    toast(data.paused ? 'Downloads paused.' : 'Downloads resumed.', 'info');
                })
                .catch(() => toast('Could not change queue state.', 'danger'));
        });
    }
    refreshQueueButton();
    refreshStorageInfo();
    const pruneBtn = document.getElementById('pruneMissing');
    if (pruneBtn) {
        pruneBtn.addEventListener('click', async () => {
            if (typeof confirmWithPhrase === 'function') {
                const typed = await confirmWithPhrase(
                    'CONFIRM',
                    'Remove history entries whose files are gone from disk.');
                if (typed === null) return;
                pruneBtn.disabled = true;
                fetch('/api/prune-missing', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json',
                               'X-CSRFToken': csrfToken() },
                    body: JSON.stringify({ confirm: typed }),
                })
                    .then((r) => r.json())
                    .then((data) => {
                        toast(data.ok ? ('Removed ' + data.removed + ' missing entr' + (data.removed === 1 ? 'y.' : 'ies.')) : (data.message || 'Cleanup failed.'),
                              data.ok ? 'success' : 'danger');
                        refreshStorageInfo();
                        if (typeof refreshTable === 'function') refreshTable();
                        else window.location.reload();
                    })
                    .catch(() => toast('Cleanup failed.', 'danger'))
                    .finally(() => { pruneBtn.disabled = false; });
                return;
            }
            if (!confirm('Remove history entries whose files are gone from disk? Active downloads are never touched.')) return;
            pruneBtn.disabled = true;
            fetch('/api/prune-missing', {
                method: 'POST',
                headers: { 'X-CSRFToken': csrfToken() },
            })
                .then((r) => r.json())
                .then((data) => {
                    toast(data.ok ? ('Removed ' + data.removed + ' missing entr' + (data.removed === 1 ? 'y.' : 'ies.')) : (data.message || 'Cleanup failed.'),
                          data.ok ? 'success' : 'danger');
                    refreshStorageInfo();
                    if (typeof refreshTable === 'function') refreshTable();
                    else window.location.reload();
                })
                .catch(() => toast('Cleanup failed.', 'danger'))
                .finally(() => { pruneBtn.disabled = false; });
        });
    }
    const verifyBtn = document.getElementById('verifyFiles');
    if (verifyBtn) {
        const pollVerify = () => {
            fetch('/api/verify-status')
                .then((r) => r.json())
                .then((data) => {
                    if (data.running) {
                        verifyBtn.textContent = 'Verifying ' + data.checked + '/' + data.total + '…';
                        setTimeout(pollVerify, 2000);
                    } else {
                        verifyBtn.textContent = 'Verify files';
                        verifyBtn.disabled = false;
                        toast(data.message || 'Verification finished.',
                              data.ok ? 'success' : 'danger');
                        if (typeof refreshTable === 'function') refreshTable();
                    }
                })
                .catch(() => { verifyBtn.textContent = 'Verify files'; verifyBtn.disabled = false; });
        };
        verifyBtn.addEventListener('click', () => {
            if (!confirm('Check every downloaded file for corruption and re-download the bad ones?')) return;
            verifyBtn.disabled = true;
            fetch('/api/verify-files', {
                method: 'POST',
                headers: { 'X-CSRFToken': csrfToken() },
            })
                .then((r) => r.json())
                .then((data) => {
                    if (!data.ok) {
                        toast(data.message || 'Verification failed.', 'danger');
                        verifyBtn.disabled = false;
                        return;
                    }
                    if (!data.started) {
                        toast(data.message || 'A scan is already running.', 'info');
                    }
                    pollVerify();
                })
                .catch(() => { toast('Verification failed.', 'danger'); verifyBtn.disabled = false; });
        });
    }
    applyHistoryFilter();
}

document.addEventListener('DOMContentLoaded', initHistoryToolbar);

// --- Command palette: Ctrl/⌘K ------------------------------------------------
// One bar for everything: paste a link to convert it, fuzzy-find library
// tracks to play/queue, or run app actions. Built on DOM the script
// injects, so no template changes are needed on any page.
(function commandPalette() {
    let overlay = null;
    let input = null;
    let list = null;
    let libCache = null;
    let libPending = null;
    let activeIdx = 0;
    let visible = [];

    function looksLikeUrl(text) {
        return /^(https?:\/\/|www\.)\S+$/i.test(text.trim());
    }

    function ensureDOM() {
        if (overlay) return;
        overlay = document.createElement('div');
        overlay.id = 'cmdPalette';
        overlay.className = 'cmd-palette d-none';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-label', 'Command palette');
        const box = document.createElement('div');
        box.className = 'cmd-palette-box';
        input = document.createElement('input');
        input.type = 'text';
        input.className = 'form-control form-control-lg';
        input.placeholder = 'Paste a link, search tracks, or type an action…';
        input.setAttribute('aria-label', 'Command palette input');
        input.setAttribute('autocomplete', 'off');
        list = document.createElement('div');
        list.className = 'cmd-palette-list';
        box.appendChild(input);
        box.appendChild(list);
        overlay.appendChild(box);
        document.body.appendChild(overlay);
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) close();
        });
        input.addEventListener('input', () => render(input.value));
        input.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                activeIdx = Math.min(visible.length - 1, activeIdx + 1);
                paint();
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                activeIdx = Math.max(0, activeIdx - 1);
                paint();
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (visible[activeIdx]) run(visible[activeIdx]);
            } else if (e.key === 'Escape') {
                close();
            }
        });
    }

    function library() {
        if (libCache) return Promise.resolve(libCache);
        if (!libPending) {
            libPending = fetch('/api/library')
                .then((r) => r.json())
                .then((data) => {
                    libCache = data.items || [];
                    return libCache;
                })
                .catch(() => [])
                .finally(() => { libPending = null; });
        }
        return libPending;
    }

    function actions() {
        const go = (label, path) => ({
            kind: 'action', label: label, hint: 'Go to ' + label,
            run: () => { window.location.href = path; },
        });
        const call = (label, hint, url, method, done) => ({
            kind: 'action', label: label, hint: hint,
            run: () => {
                fetch(url, { method: method, headers: { 'X-CSRFToken': csrfToken() } })
                    .then((r) => r.json())
                    .then((d) => toast(d.ok ? (done || label + ' done.') : (d.message || 'Failed.'), d.ok ? 'success' : 'danger'))
                    .catch(() => toast('Failed.', 'danger'));
            },
        });
        const post = (label, hint, url, done) => call(label, hint, url, 'POST', done);
        const get = (label, hint, url, done) => call(label, hint, url, 'GET', done);
        return [
            go('Player', '/player'),
            go('History', '/history'),
            go('Video', '/video'),
            go('Settings', '/settings'),
            go('Home', '/'),
            get('Pause queue', 'Pause all downloads', '/api/queue/pause', 'Queue paused.'),
            get('Resume queue', 'Resume all downloads', '/api/queue/resume', 'Queue resumed.'),
            post('Verify files', 'Re-check saved files', '/api/verify-files', 'Verification started.'),
            post('Prune missing files', 'Drop rows whose files are gone', '/api/prune-missing', 'Pruned.'),
        ];
    }

    function render(filter) {
        const q = filter.trim().toLowerCase();
        const rows = [];
        if (looksLikeUrl(filter)) {
            rows.push({
                kind: 'convert', label: 'Convert ' + filter.trim(), hint: 'Queue this link',
                url: filter.trim(),
                run: (item) => {
                    const form = new FormData();
                    form.append('csrf_token', csrfToken());
                    form.append('url', item.url);
                    const fmt = document.querySelector('#format');
                    form.append('format', (fmt && fmt.value) || 'flac');
                    const out = document.querySelector('#output_path, #video_output_path');
                    if (out && out.value) form.append('output_path', out.value);
                    fetch('/convert', { method: 'POST', body: form })
                        .then(() => {
                            toast('Conversion queued. Track progress on Home.', 'success');
                            if (window.location.pathname !== '/') window.location.href = '/';
                            else if (typeof refreshTable === 'function') refreshTable();
                        })
                        .catch(() => toast('Could not queue.', 'danger'));
                },
            });
        }
        const acts = actions().filter((a) => !q || a.label.toLowerCase().includes(q));
        acts.forEach((a) => rows.push(a));
        library().then((items) => {
            if (q) {
                items
                    .filter((it) => ((it.filename || '') + ' ' + (it.tag_title || '') + ' ' +
                                     (it.tag_artist || '')).toLowerCase().includes(q))
                    .slice(0, 8)
                    .forEach((it) => rows.push({
                        kind: 'track', label: (it.filename || 'Track').replace(/\.[^.]+$/, ''),
                        hint: (it.format || '') + ' · play',
                        id: it.id,
                        run: (item) => {
                            fetch('/api/track/' + item.id)
                                .then((r) => r.json())
                                .then((data) => {
                                    if (data.ok && window.AudioPlayer) {
                                        window.AudioPlayer.playItems([data.item], 0, '');
                                    }
                                })
                                .catch(() => toast('Could not play.', 'danger'));
                        },
                    }));
            }
            visible = rows.slice(0, 12);
            activeIdx = 0;
            paint();
        });
        visible = rows.slice(0, 12);
        activeIdx = 0;
        paint();
    }

    function paint() {
        list.innerHTML = '';
        if (!visible.length) {
            list.innerHTML = '<div class="text-muted small p-2">No matches.</div>';
            return;
        }
        visible.forEach((item, i) => {
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'cmd-row' + (i === activeIdx ? ' active' : '');
            const label = document.createElement('span');
            label.textContent = item.label;
            row.appendChild(label);
            if (item.hint) {
                const hint = document.createElement('span');
                hint.className = 'cmd-hint';
                hint.textContent = item.hint;
                row.appendChild(hint);
            }
            row.addEventListener('click', () => run(item));
            row.addEventListener('mousemove', () => {
                if (activeIdx !== i) {
                    activeIdx = i;
                    paint();
                }
            });
            list.appendChild(row);
        });
    }

    function run(item) {
        close();
        try {
            item.run(item);
        } catch (err) { /* ignore */ }
    }

    function open() {
        ensureDOM();
        libCache = null;
        overlay.classList.remove('d-none');
        input.value = '';
        render('');
        setTimeout(() => input.focus(), 0);
    }

    function close() {
        if (overlay) overlay.classList.add('d-none');
    }

    document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
            e.preventDefault();
            ensureDOM();
            if (overlay.classList.contains('d-none')) open();
            else close();
        } else if (e.key === 'Escape' && overlay && !overlay.classList.contains('d-none')) {
            close();
        }
    });
})();

// --- Convert local files: dropzone + file picker -----------------------------
// Dropped/chosen files upload to /api/upload-convert (loopback-fast) and
// queue as conversion jobs; progress shows in History like any download.
(function localConvert() {
    function init() {
        const zone = document.getElementById('fileDropzone');
        if (!zone) return;
        const input = document.getElementById('fileBrowseInput');
        const fmtSel = document.getElementById('localFormat');
        const browse = document.getElementById('fileBrowseBtn');
        if (browse && input) {
            browse.addEventListener('click', (e) => {
                e.stopPropagation();
                input.click();
            });
        }
        zone.addEventListener('click', (e) => {
            if (input && e.target !== browse) input.click();
        });
        zone.addEventListener('keydown', (e) => {
            if ((e.key === 'Enter' || e.key === ' ') && input) {
                e.preventDefault();
                input.click();
            }
        });
        ['dragenter', 'dragover'].forEach((ev) => {
            zone.addEventListener(ev, (e) => {
                e.preventDefault();
                zone.classList.add('dragging');
            });
        });
        ['dragleave', 'drop'].forEach((ev) => {
            zone.addEventListener(ev, (e) => {
                e.preventDefault();
                zone.classList.remove('dragging');
            });
        });
        zone.addEventListener('drop', (e) => {
            const files = (e.dataTransfer && e.dataTransfer.files) || [];
            if (files.length) uploadLocalFiles(files);
        });
        if (input) {
            input.addEventListener('change', () => {
                if (input.files && input.files.length) uploadLocalFiles(input.files);
                input.value = '';
            });
        }

        function targetFormat() {
            return fmtSel ? fmtSel.value : 'flac';
        }

        function uploadLocalFiles(files) {
            const form = new FormData();
            form.append('csrf_token', csrfToken());
            form.append('format', targetFormat());
            let count = 0;
            Array.from(files).slice(0, 50).forEach((f) => {
                form.append('files', f, f.name);
                count += 1;
            });
            if (!count) return;
            toast('Uploading ' + count + ' file(s)…', 'info');
            fetch('/api/upload-convert', { method: 'POST', body: form })
                .then((r) => r.json())
                .then((data) => {
                    toast(data.ok ? (data.message || 'Queued.') : (data.message || 'Could not convert.'),
                          data.ok ? 'success' : 'danger');
                    if (data.ok && typeof refreshTable === 'function') refreshTable();
                })
                .catch(() => toast('Upload failed.', 'danger'));
        }
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

// --- First-run wizard --------------------------------------------------------
// Shows once (until the first conversion exists) on Home. Dismissal lasts
// the browser session; a queued conversion retires it permanently.
(function firstRun() {
    // Modal welcome tour: overlay with three steps, skippable anywhere.
    // Shows once (until the first conversion exists); dismissal lasts the
    // browser session, a queued conversion retires it permanently.
    function init() {
        const overlay = document.getElementById('firstRunTour');
        if (!overlay) return;
        const dismiss = () => {
            overlay.classList.add('d-none');
            try {
                sessionStorage.setItem('firstRunDismissed', '1');
            } catch (err) { /* private mode */ }
        };
        try {
            if (sessionStorage.getItem('firstRunDismissed')) return;
        } catch (err) { /* private mode */ }
        const steps = Array.from(overlay.querySelectorAll('[data-tour-step]'));
        const dots = Array.from(overlay.querySelectorAll('[data-tour-dot]'));
        let current = 0;
        const show = (i) => {
            current = Math.max(0, Math.min(steps.length - 1, i));
            steps.forEach((el, idx) => el.classList.toggle('d-none', idx !== current));
            dots.forEach((el, idx) => el.classList.toggle('on', idx <= current));
        };
        overlay.querySelectorAll('[data-tour-next]').forEach((btn) => {
            btn.addEventListener('click', () => show(current + 1));
        });
        overlay.querySelectorAll('[data-tour-back]').forEach((btn) => {
            btn.addEventListener('click', () => show(current - 1));
        });
        overlay.querySelectorAll('.tour-skip').forEach((btn) => {
            btn.addEventListener('click', dismiss);
        });
        overlay.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') dismiss();
        });
        fetch('/api/first-run')
            .then((r) => r.json())
            .then((data) => {
                if (data.first_run) {
                    show(0);
                    overlay.classList.remove('d-none');
                    // Record it server-side: first launch only, forever.
                    fetch('/api/first-run/seen', {
                        method: 'POST',
                        headers: { 'X-CSRFToken': csrfToken() },
                    }).catch(() => {});
                }
            })
            .catch(() => {});
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

// --- What's new: one dialog per app version ---------------------------------
(function whatsNew() {
    function init() {
        try {
            if (sessionStorage.getItem('whatsNewSeen')) return;
        } catch (err) { /* private mode */ }
        fetch('/api/whats-new')
            .then((r) => r.json())
            .then((data) => {
                if (!data.is_new) return;
                try {
                    sessionStorage.setItem('whatsNewSeen', data.version || '1');
                } catch (err) { /* private mode */ }
                if (typeof toast === 'function') {
                    toast('Updated to ' + data.version + ' — see what changed.', 'info');
                }
                if (confirm('Audio Converter updated to ' + data.version + '.\n\nOpen the release notes?')) {
                    window.open(data.url, '_blank', 'noopener');
                }
            })
            .catch(() => {});
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
