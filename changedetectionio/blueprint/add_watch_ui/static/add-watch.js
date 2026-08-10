// Add Watch UI glue: fetch a live snapshot for the entered URL and drive the
// shared visual selector (window.initVisualSelector from visual-selector.js).

$(document).ready(() => {
    const $url = $('#url');
    const $go = $('#add-watch-go');
    const $emptyState = $('#add-watch-empty-state');
    const $spinner = $('#add-watch-spinner');
    const $error = $('#add-watch-error');
    const $wrapper = $('#selector-wrapper');
    const $xpathRow = $('#selector-current-xpath');
    const $byElement = $('#by-element-toggle');
    const $clear = $('#clear-selector');
    const $includeFilters = $('#include_filters');
    const $temporaryUuid = $('#temporary_uuid');

    const vs = window.initVisualSelector({
        $canvas: $('#selector-canvas'),
        $includeFilters: $includeFilters,
        $background: $('#selector-background'),
        $xpathDisplay: $('#selector-current-xpath span'),
        $fetchingNotice: $('#add-watch-spinner .fetching-update-notice'),
        $wrapper: $wrapper,
        $clearButton: $clear,
        enableSelection: false, // off until the user opts into "Select by element"
        processorIsImage: false,
        // The snapshot comes from the live browser-steps capture, so scale X by the page
        // CSS width (browser_width) like browser-steps.js - handles device-scale-factor != 1.
        scaleByBrowserWidth: true,
    });

    function showState(which) {
        // which: 'empty' | 'loading' | 'error' | 'ready'
        $emptyState.toggle(which === 'empty');
        $spinner.toggle(which === 'loading');
        $error.toggle(which === 'error');
        const ready = which === 'ready';
        $wrapper.toggle(ready);
        $xpathRow.toggle(ready && $byElement.is(':checked'));
        $clear.toggle(ready && $byElement.is(':checked'));
    }

    function fetchSnapshot() {
        const url = ($url.val() || '').trim();
        if (!url) {
            $url.focus();
            return;
        }

        showState('loading');
        // A previous parked snapshot is now stale; drop it until this fetch succeeds.
        $temporaryUuid.val('');

        $.ajax({
            url: add_watch_snapshot_url,
            data: {url: url},
            dataType: 'json',
        }).done((data) => {
            showState('ready');
            $temporaryUuid.val(data.temporary_uuid || '');
            vs.load({screenshotSrc: data.screenshot, xpathData: data.xpath_data});
        }).fail((xhr) => {
            const msg = (xhr && xhr.responseText) ? xhr.responseText : 'Could not fetch a preview for that URL.';
            $error.text(msg);
            showState('error');
        });
    }

    $go.on('click', fetchSnapshot);

    // Prove-before-promise: ask the stealth validator whether we can reliably watch
    // this URL, and show the verdict (and which anti-bot wall, if blocked) before the
    // user commits to adding it.
    const $check = $('#add-watch-check');
    const $watchable = $('#add-watch-watchable');
    function checkWatchable() {
        const url = ($url.val() || '').trim();
        if (!url) { $url.focus(); return; }
        $check.prop('disabled', true);
        $watchable.show()
            .css({background: '#eef2ff', color: '#333', border: '1px solid #c7d2fe'})
            .text('Checking whether this page is watchable…');
        $.ajax({url: add_watch_validate_url, data: {url: url}, dataType: 'json'})
            .done((d) => {
                const good = !!d.watchable;
                $watchable.css({
                    background: good ? '#ecfdf5' : '#fef2f2',
                    color: good ? '#065f46' : '#991b1b',
                    border: '1px solid ' + (good ? '#a7f3d0' : '#fecaca')
                }).text(d.message || (good ? 'Watchable.' : 'This page could not be verified.'));
            })
            .fail((xhr) => {
                let m = 'Could not check this page.';
                try { m = (JSON.parse(xhr.responseText).message) || m; } catch (e) {}
                $watchable.css({background: '#fef2f2', color: '#991b1b', border: '1px solid #fecaca'}).text(m);
            })
            .always(() => $check.prop('disabled', false));
    }
    $check.on('click', checkWatchable);

    // Enter in the URL box should fetch a preview, not submit the whole form
    $url.on('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            fetchSnapshot();
        }
    });

    // "Select by element" toggles live hover/click element selection
    $byElement.on('change', function () {
        const on = $(this).is(':checked');
        vs.setSelectionEnabled(on);
        $xpathRow.toggle(on && $wrapper.is(':visible'));
        $clear.toggle(on && $wrapper.is(':visible'));
        if (!on) {
            $includeFilters.val('');
        }
    });

    showState('empty');
});
