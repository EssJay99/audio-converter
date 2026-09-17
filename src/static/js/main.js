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
function renderSavedFileCell(cell, item) {
    cell.innerHTML = '';

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
    const skipBtn = e.target.closest('[data-skip]');
    if (skipBtn) {
        skipBtn.disabled = true;
        fetch('/api/skip/' + skipBtn.dataset.skip)
            .then((r) => r.json())
            .then((data) => {
                if (!data.ok) {
                    if (data.message) alert(data.message);
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
                alert('Could not skip this track.');
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
                    if (data.message) alert(data.message);
                    delPlBtn.disabled = false;
                    return;
                }
                const row = delPlBtn.closest('tr');
                const box = row && row.parentElement
                    ? row.parentElement.querySelector('#children-' + delPlBtn.dataset.deletePlaylist)
                    : null;
                if (box) box.remove();
                if (row) row.remove();
                toast('Deleted ' + data.removed_tracks + ' track(s).', 'info');
                refreshStorageInfo();
                if (typeof refreshTable === 'function') refreshTable();
            })
            .catch(() => {
                alert('Could not delete this playlist.');
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
        if (!confirm('Delete this file from disk and remove it from history?')) {
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
                    if (data.message) alert(data.message);
                    delBtn.disabled = false;
                    return;
                }
                const row = delBtn.closest('tr');
                if (row) row.remove();
                toast(data.removed_file ? 'Deleted file from disk.' : 'Removed from history.', 'info');
                refreshStorageInfo();
                if (typeof refreshTable === 'function') refreshTable();
            })
            .catch(() => {
                alert('Could not delete this track.');
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
                    if (data.message) alert(data.message);
                    retryBtn.disabled = false;
                    return;
                }
                toast('Download re-queued.', 'success');
                if (typeof refreshTable === 'function') refreshTable();
            })
            .catch(() => {
                alert('Could not retry this track.');
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
                    if (data.message) alert(data.message);
                    return;
                }
                toast('Renamed to ' + data.filename + '.', 'success');
                if (typeof refreshTable === 'function') refreshTable();
                else window.location.reload();
            })
            .catch(() => alert('Could not rename this file.'));
        e.preventDefault();
        return;
    }
    const tagsBtn = e.target.closest('[data-tags]');
    if (tagsBtn) {
        if (typeof bootstrap === 'undefined') {
            alert('Tag editor unavailable.');
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
                                alert(saved.message || 'Could not save tags.');
                                return;
                            }
                            modal.hide();
                            toast('Tags updated.', 'success');
                        })
                        .catch(() => alert('Could not save tags.'));
                };
                modal.show();
            })
            .catch(() => alert('Could not load current tags.'));
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
                    if (data.message) alert(data.message);
                } else {
                    toast('Queued ' + data.queued + ' newly matched track(s).' +
                        (data.remaining ? ' ' + data.remaining + ' still unmatched.' : ''), 'success');
                }
                missesBtn.disabled = false;
                if (typeof refreshTable === 'function') refreshTable();
            })
            .catch(() => {
                alert('Could not retry unmatched tracks.');
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
                if (!data.ok && data.message) alert(data.message);
            })
            .catch(() => alert('Could not open your file manager.'))
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
function renderHistoryRow(item) {
    const tr = document.createElement('tr');
    tr.id = 'row-' + item.id;
    if (item.parent_id) tr.dataset.playlistId = item.parent_id;
    if (item.output_path) tr.dataset.folder = item.output_path;

    const statusTd = document.createElement('td');
    statusTd.className = 'row-status';
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
        (tr) => tr.tagName === 'TR' && !tr.classList.contains('playlist-children'));
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
                .catch(() => alert('Could not change queue state.'));
        });
    }
    refreshQueueButton();
    refreshStorageInfo();
    initServerSearch();
    const verifyBtn = document.getElementById('verifyFiles');
    if (verifyBtn) {
        verifyBtn.addEventListener('click', () => {
            if (!confirm('Check every downloaded file for corruption and re-download the bad ones?')) return;
            verifyBtn.disabled = true;
            fetch('/api/verify-files', {
                method: 'POST',
                headers: { 'X-CSRFToken': csrfToken() },
            })
                .then((r) => r.json())
                .then((data) => {
                    toast(data.message || 'Verification finished.',
                          data.ok ? 'success' : 'danger');
                    if (typeof refreshTable === 'function') refreshTable();
                })
                .catch(() => toast('Verification failed.', 'danger'))
                .finally(() => { verifyBtn.disabled = false; });
        });
    }
    applyHistoryFilter();
}

document.addEventListener('DOMContentLoaded', initHistoryToolbar);

function startProgressBar(elementIds) {
    const bar = document.getElementById(elementIds.bar);
    const msg = document.getElementById(elementIds.message);
    const progress = document.getElementById(elementIds.progress);
    if (!bar || !progress) return;

    progress.style.display = 'block';
    bar.style.width = '10%';
    if (msg) msg.textContent = 'Starting download...';

    let width = 10;
    const timer = setInterval(() => {
        width = Math.min(width + 2, 90);
        bar.style.width = width + '%';
    }, 1500);

    window.setTimeout(() => clearInterval(timer), 120000);
}

function pollConversionStatus(url, onComplete, onFailed) {    const pollInterval = setInterval(() => {
        fetch(`/convert/status?url=${encodeURIComponent(url)}`)
            .then(response => {
                if (!response.ok) return null;
                return response.json();
            })
            .then(data => {
                if (!data) return;
                if (data.status === 'completed' && onComplete) onComplete(data);
                else if (data.status === 'failed' && onFailed) {
                    onFailed(data);
                    clearInterval(pollInterval);
                }
            })
            .catch(() => {});
    }, 5000);

    setTimeout(() => clearInterval(pollInterval), 300000);
}

// --- Dark mode -------------------------------------------------------------

function initDarkMode() {
    const apply = (dark) => {
        document.body.classList.toggle('dark-mode', dark);
        try {
            localStorage.setItem('appDarkMode', dark ? '1' : '0');
        } catch (err) { /* private mode */ }
        const btn = document.getElementById('darkModeToggle');
        if (btn) btn.textContent = dark ? '☀' : '☾';
    };
    let dark = false;
    try {
        dark = localStorage.getItem('appDarkMode') === '1';
    } catch (err) { /* private mode */ }
    const nav = document.querySelector('.navbar .container-fluid');
    if (nav && !document.getElementById('darkModeToggle')) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.id = 'darkModeToggle';
        btn.className = 'btn btn-sm btn-outline-light ms-auto';
        btn.title = 'Toggle dark mode';
        btn.setAttribute('aria-label', 'Toggle dark mode');
        btn.addEventListener('click', () => {
            apply(!document.body.classList.contains('dark-mode'));
        });
        nav.appendChild(btn);
    }
    apply(dark);
}

document.addEventListener('DOMContentLoaded', initDarkMode);

// --- Drag and drop links ---------------------------------------------------

function initDragDrop() {
    const urlInput = document.getElementById('url');
    if (!urlInput) return;
    document.addEventListener('dragover', (e) => {
        if (e.target.closest && e.target.closest('input, textarea, select')) return;
        e.preventDefault();
        document.body.classList.add('drag-over');
    });
    document.addEventListener('dragleave', (e) => {
        if (e.relatedTarget === null) document.body.classList.remove('drag-over');
    });
    document.addEventListener('drop', (e) => {
        if (e.target.closest && e.target.closest('input, textarea, select')) return;
        document.body.classList.remove('drag-over');
        const data = e.dataTransfer
            ? (e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain'))
            : '';
        const match = String(data || '').match(/https?:\/\/[^\s]+/);
        if (match) {
            e.preventDefault();
            urlInput.value = match[0];
            urlInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
            urlInput.focus();
            toast('Link ready — pick a format and hit Convert.', 'info');
        }
    });
}

document.addEventListener('DOMContentLoaded', initDragDrop);