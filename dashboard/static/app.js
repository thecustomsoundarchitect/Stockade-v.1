const service = document.querySelector('#service');
const cycle = document.querySelector('#cycle');
const cycleTime = document.querySelector('#cycle-time');
const cycleDetail = document.querySelector('#cycle-detail');
const cycleFailureCount = document.querySelector('#cycle-failure-count');
const connection = document.querySelector('#connection');
const reportSummary = document.querySelector('#report-summary');
const reportDetail = document.querySelector('#report-detail');
const failureEmailStatus = document.querySelector('#failure-email-status');
const downtimeEmailStatus = document.querySelector('#downtime-email-status');
const emailStatus = document.querySelector('#email-status');
const reportEmailStatus = document.querySelector('#report-email-status');
const quarantineSummary = document.querySelector('#quarantine-summary');
const quarantineTime = document.querySelector('#quarantine-time');
const quarantineList = document.querySelector('#quarantine-list');
const saleSummary = document.querySelector('#sale-summary');
const saleList = document.querySelector('#sale-list');

async function refresh() {
  try {
    const response = await fetch('/api/status', { cache: 'no-store' });
    if (!response.ok) throw new Error('Status unavailable');
    const data = await response.json();
    connection.hidden = !['delayed', 'waiting'].includes(data.connection);
    if (data.connection === 'delayed') connection.textContent = 'The connection to the iMac is delayed. The sync and quarantine details may be old.';
    if (data.connection === 'waiting') connection.textContent = 'Waiting for the iMac to send its first update.';
    service.textContent = { running: 'Running', stopped: 'Stopped', unavailable: 'Unavailable' }[data.service] || 'Unavailable';
    const failedCycles = data.last_cycle?.consecutive_failures;
    cycleFailureCount.textContent = data.last_cycle?.result === 'failed' && Number.isInteger(failedCycles) && failedCycles > 0
      ? `${failedCycles} failed sync ${failedCycles === 1 ? 'cycle' : 'cycles'} in a row` : '';
    cycleFailureCount.hidden = !cycleFailureCount.textContent;
    if (data.last_cycle) {
      cycle.textContent = data.last_cycle.result === 'ok' ? 'Successful' : 'Failed';
      cycleTime.textContent = new Date(data.last_cycle.time * 1000).toLocaleString();
      cycleDetail.textContent = data.last_cycle.result === 'failed' && data.last_cycle.error ? data.last_cycle.error : '';
      cycleDetail.hidden = !cycleDetail.textContent;
    } else {
      cycle.textContent = 'Unavailable';
      cycleTime.textContent = '';
      cycleDetail.hidden = true;
    }
    if (data.sync_report && data.sync_report.text) {
      reportSummary.textContent = 'Latest observed changes';
      reportDetail.textContent = data.sync_report.text;
      reportDetail.hidden = false;
    } else {
      reportSummary.textContent = 'No report available';
      reportDetail.hidden = true;
    }
    downtimeEmailStatus.textContent = ({ configured: 'Configured', sent: 'Sent', error: 'Unable to send' })[data.downtime_email] || 'Unavailable';
    failureEmailStatus.textContent = ({ configured: 'Configured', sent: 'Sent', error: 'Unable to send' })[data.failure_email] || 'Unavailable';
    emailStatus.textContent = ({ configured: 'Configured', sent: 'Sent', error: 'Unable to send' })[data.quarantine_email] || 'Unavailable';
    reportEmailStatus.textContent = ({ configured: 'Configured', sent: 'Sent', error: 'Unable to send' })[data.sync_email] || 'Unavailable';
    quarantineList.replaceChildren();
    if (data.quarantine && Array.isArray(data.quarantine.items)) {
      const items = data.quarantine.items;
      quarantineSummary.textContent = items.length === 1 ? '1 item' : `${items.length} items`;
      quarantineTime.textContent = 'Updated ' + new Date(data.quarantine.time * 1000).toLocaleString();
      for (const item of items) {
        const reasons = item.reasons.map(reason => ({
          unknown_brickowl_catalogue: 'Item not found in the Brick Owl catalogue',
          sale_percentage: 'Sale percentage could not be applied',
          unreadable_public_note: 'Unreadable public Note'
        })[reason] || 'Reason unavailable');
        const row = document.createElement('li');
        row.textContent = `BrickLink lot ${item.lot_id}: ${reasons.join(', ')}`;
        quarantineList.appendChild(row);
      }
    } else {
      quarantineSummary.textContent = 'Unavailable';
      quarantineTime.textContent = 'The quarantine count and item list are unavailable.';
    }
    saleList.replaceChildren();
    if (Array.isArray(data.sale_skips)) {
      saleSummary.textContent = data.sale_skips.length === 1 ? '1 recorded item' : `${data.sale_skips.length} recorded items`;
      for (const item of data.sale_skips) {
        const row = document.createElement('li');
        row.textContent = `BrickLink lot ${item.lot}, sale ${item.percent}%`;
        saleList.appendChild(row);
      }
    } else {
      saleSummary.textContent = 'Unavailable';
    }
  } catch (_) {
    connection.hidden = false;
    connection.textContent = 'Dashboard connection unavailable. Trying again.';
    service.textContent = 'Unavailable';
    cycle.textContent = 'Unavailable';
    cycleTime.textContent = '';
    cycleDetail.hidden = true;
    cycleFailureCount.textContent = '';
    cycleFailureCount.hidden = true;
    reportSummary.textContent = 'Unavailable';
    reportDetail.hidden = true;
    downtimeEmailStatus.textContent = 'Unavailable';
    failureEmailStatus.textContent = 'Unavailable';
    emailStatus.textContent = 'Unavailable';
    reportEmailStatus.textContent = 'Unavailable';
    quarantineSummary.textContent = 'Unavailable';
    quarantineTime.textContent = 'The quarantine count and item list are unavailable.';
    quarantineList.replaceChildren();
    saleSummary.textContent = 'Unavailable';
    saleList.replaceChildren();
  }
}
refresh();
setInterval(refresh, 15000);

function setupLotControls() {
  if (location.protocol !== 'http:' || !['localhost', '127.0.0.1'].includes(location.hostname)) return;
  const controls = document.querySelector('#lot-controls');
  const form = document.querySelector('#lot-form');
  const id = document.querySelector('#lot-id');
  const fields = document.querySelector('#lot-fields');
  const note = document.querySelector('#lot-note');
  const noteUnreadable = document.querySelector('#lot-note-unreadable');
  const remark = document.querySelector('#lot-remark');
  const remarkState = document.querySelector('#lot-remark-state');
  const load = document.querySelector('#load-lot');
  const save = document.querySelector('#save-lot-note');
  const skip = document.querySelector('#skip-lot-owl');
  const message = document.querySelector('#lot-message');
  let selected = '';

  function showMessage(text) {
    message.textContent = text;
    message.hidden = !text;
  }

  function showRemark(value) {
    const readable = typeof value === 'string';
    remark.textContent = readable ? value : '';
    remark.hidden = !remark.textContent;
    remarkState.textContent = readable ? 'Blank' : 'Unreadable';
    remarkState.hidden = !remark.hidden;
  }

  async function run(action) {
    const lotId = action === 'lookup' ? id.value : selected;
    if (!lotId || load.disabled) return;
    if (action === 'lookup') {
      selected = '';
      fields.hidden = true;
    }
    for (const control of [id, note, load, save, skip]) control.disabled = true;
    showMessage('Waiting for BrickSync.');
    try {
      const request = { lot_id: lotId, action };
      if (action === 'edit_note') request.note = note.value;
      const response = await fetch('/api/lot', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request)
      });
      const queued = await response.json();
      if (!response.ok) throw new Error(queued.message || 'Stockade could not queue the request.');

      const deadline = Date.now() + 60000;
      while (Date.now() < deadline) {
        const reply = await fetch('/api/lot-result?request_id=' + encodeURIComponent(queued.request_id), { cache: 'no-store' });
        const result = await reply.json();
        if (reply.status === 202) {
          await new Promise(resolve => setTimeout(resolve, 1000));
          continue;
        }
        if (!reply.ok || result.request_id !== queued.request_id || result.status !== 'ok') {
          throw new Error(result.message || 'BrickSync could not complete the request.');
        }
        const readableNote = typeof result.note === 'string';
        if (action === 'lookup' || (action === 'edit_note' && readableNote)) {
          note.value = readableNote ? result.note : '';
          noteUnreadable.hidden = readableNote;
        }
        if (action !== 'skip_owl' || (typeof result.remark === 'string' && result.remark)) {
          showRemark(result.remark);
        }
        selected = lotId;
        fields.hidden = false;
        showMessage(result.message || 'Lot updated.');
        return;
      }
      selected = '';
      fields.hidden = true;
      showMessage('BrickSync has not returned a result. Load the lot again before making another change.');
    } catch (error) {
      showMessage(error.message || 'Stockade could not complete the request.');
    } finally {
      for (const control of [id, note, load, save, skip]) control.disabled = false;
    }
  }

  id.addEventListener('input', () => {
    selected = '';
    fields.hidden = true;
    showMessage('');
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    return run('lookup');
  });
  save.addEventListener('click', () => run('edit_note'));
  skip.addEventListener('click', () => run('skip_owl'));
  controls.hidden = false;
}
setupLotControls();
