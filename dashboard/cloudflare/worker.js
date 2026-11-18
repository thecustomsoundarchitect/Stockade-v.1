import { DurableObject } from 'cloudflare:workers';
import { connect } from 'cloudflare:sockets';
import { createRemoteJWKSet, jwtVerify } from 'jose';
import { Buffer } from 'node:buffer';

const encoder = new TextEncoder();
const headers = { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' };
const mailbox = 'info@rtqlife.com';

function emailReady(env) {
  return Boolean(env.HOSTINGER_SMTP_PASSWORD);
}

async function sendEmail(env, subject, text) {
  if (!emailReady(env)) throw new Error('Hostinger SMTP password is unavailable.');
  if (/[^\x20-\x7e]/.test(subject)) throw new Error('Invalid email subject.');

  const socket = connect({ hostname: 'smtp.hostinger.com', port: 465 }, { secureTransport: 'on' });
  socket.closed.catch(() => {});
  let accepted = false;
  let timeout;

  try {
    const send = async () => {
      await socket.opened;
      const reader = socket.readable.getReader();
      const writer = socket.writable.getWriter();
      const decoder = new TextDecoder();
      let pending = '';

      async function readReply(expected) {
        let code;
        let bytesRead = 0;
        const lines = [];
        while (true) {
          const end = pending.indexOf('\r\n');
          if (end === -1) {
            const { done, value } = await reader.read();
            if (done) throw new Error('Hostinger closed the SMTP connection.');
            bytesRead += value.byteLength;
            if (bytesRead > 16_384) throw new Error('Hostinger SMTP reply is too long.');
            pending += decoder.decode(value, { stream: true });
            continue;
          }
          const line = pending.slice(0, end);
          pending = pending.slice(end + 2);
          const match = /^(\d{3})(?:([ -])(.*))?$/.exec(line);
          const lineCode = Number(match?.[1]);
          if (!expected.includes(lineCode) || (code !== undefined && code !== lineCode)) {
            throw new Error('Hostinger rejected an SMTP command.');
          }
          code = lineCode;
          lines.push(match[3] ?? '');
          if (match[2] !== '-') return lines;
        }
      }

      async function command(line, ...expected) {
        await writer.write(encoder.encode(`${line}\r\n`));
        return readReply(expected);
      }

      await readReply([220]);
      const capabilities = await command('EHLO rtqlife.com', 250);
      const methods = capabilities.flatMap(line => {
        const match = /^AUTH[ =]+(.+)$/i.exec(line);
        return match ? match[1].toUpperCase().split(/\s+/) : [];
      });
      const password = env.HOSTINGER_SMTP_PASSWORD;
      if (methods.includes('PLAIN')) {
        const credentials = Buffer.from(`\0${mailbox}\0${password}`, 'utf8').toString('base64');
        await command(`AUTH PLAIN ${credentials}`, 235);
      } else if (methods.includes('LOGIN')) {
        await command('AUTH LOGIN', 334);
        await command(Buffer.from(mailbox, 'utf8').toString('base64'), 334);
        await command(Buffer.from(password, 'utf8').toString('base64'), 235);
      } else {
        throw new Error('Hostinger advertised no supported SMTP authentication method.');
      }

      await command(`MAIL FROM:<${mailbox}>`, 250);
      await command(`RCPT TO:<${mailbox}>`, 250, 251);
      await command('DATA', 354);
      const body = Buffer.from(text.replace(/\r\n|\r|\n/g, '\r\n'), 'utf8').toString('base64');
      const content = [
        `From: ${mailbox}`,
        `To: ${mailbox}`,
        `Subject: ${subject}`,
        `Date: ${new Date().toUTCString().replace(/GMT$/, '+0000')}`,
        'MIME-Version: 1.0',
        'Content-Type: text/plain; charset=UTF-8',
        'Content-Transfer-Encoding: base64',
        '',
        ...(body.match(/.{1,76}/g) ?? []),
        '.',
        '',
      ];
      // Base64 body lines cannot be mistaken for the SMTP DATA terminator.
      await writer.write(encoder.encode(content.join('\r\n')));
      await readReply([250]);
      accepted = true;
      await command('QUIT', 221);
    };

    await Promise.race([
      send(),
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error('Hostinger SMTP connection timed out.')), 20_000);
      }),
    ]);
  } catch (error) {
    // A failure after the DATA acknowledgment must not queue the same message again.
    if (!accepted) throw error;
  } finally {
    clearTimeout(timeout);
    await socket.close().catch(() => {});
  }
}

async function accessAllowed(request, env) {
  const token = request.headers.get('Cf-Access-Jwt-Assertion');
  if (!token || !env.TEAM_DOMAIN || !env.POLICY_AUD) return false;
  try {
    const domain = new URL(env.TEAM_DOMAIN);
    if (domain.protocol !== 'https:') return false;
    const keys = createRemoteJWKSet(new URL('/cdn-cgi/access/certs', domain));
    await jwtVerify(token, keys, { issuer: domain.origin, audience: env.POLICY_AUD });
    return true;
  } catch (_) {
    return false;
  }
}

async function sameSecret(provided, expected) {
  if (!expected) return false;
  const [left, right] = await Promise.all([
    crypto.subtle.digest('SHA-256', encoder.encode(provided)),
    crypto.subtle.digest('SHA-256', encoder.encode(expected)),
  ]);
  return crypto.subtle.timingSafeEqual(left, right);
}

export class SnapshotStore extends DurableObject {
  async notifyDowntime(health, message) {
    if (!emailReady(this.env)) return null;
    if (health.alerted) return 'sent';
    try {
      await sendEmail(this.env, 'Stockade downtime alert', message);
      health.alerted = true;
      return 'sent';
    } catch (_) {
      console.error('Stockade downtime email could not be sent.');
      return 'error';
    }
  }

  async observeHealth(snapshot, now) {
    const interval = 15 * 60 * 1000;
    const health = await this.ctx.storage.get('watchdog') || {
      cycleTime: null, cycleSeen: now, failureSince: null, notRunningSince: null, alerted: false,
    };
    const cycle = snapshot.last_cycle;
    if (cycle && cycle.time !== health.cycleTime) {
      health.cycleTime = cycle.time;
      health.cycleSeen = now;
    }
    if (snapshot.service === 'running') health.notRunningSince = null;
    else if (health.notRunningSince === null) health.notRunningSince = now;
    if (cycle?.result === 'ok') health.failureSince = null;
    else if (cycle?.result === 'failed' && health.failureSince === null) health.failureSince = now;
    if (snapshot.service === 'running' && cycle?.result === 'ok' && now - health.cycleSeen < interval) health.alerted = false;
    let message = null;
    if (health.notRunningSince !== null && now - health.notRunningSince >= interval)
      message = 'Stockade has not reported a running service for 15 minutes.';
    else if (health.failureSince !== null && now - health.failureSince >= interval)
      message = 'Stockade has not reported a successful sync for 15 minutes.';
    else if (now - health.cycleSeen >= interval)
      message = 'Stockade has not reported a completed sync attempt for 15 minutes.';
    let emailStatus = emailReady(this.env) ? (health.alerted ? 'sent' : 'configured') : null;
    if (message) emailStatus = await this.notifyDowntime(health, message);
    await this.ctx.storage.put('watchdog', health);
    return emailStatus;
  }

  async alarm() {
    const status = await this.ctx.storage.get('status');
    if (!status) return;
    const interval = 15 * 60 * 1000;
    const now = Date.now();
    if (now - status.received < interval) {
      await this.ctx.storage.setAlarm(status.received + interval);
      return;
    }
    const health = await this.ctx.storage.get('watchdog') || { alerted: false };
    const emailStatus = await this.notifyDowntime(health, 'Stockade has not reported for 15 minutes.');
    await this.ctx.storage.put('watchdog', health);
    await this.ctx.storage.put('status', { ...status, snapshot: { ...status.snapshot, downtime_email: emailStatus } });
    if (emailStatus !== 'sent') await this.ctx.storage.setAlarm(now + (emailStatus === 'error' ? 60000 : interval));
  }

  async fetch(request) {
    if (request.method === 'PUT') {
      const snapshot = await request.json();
      const mailReady = emailReady(this.env);
      const quarantineCount = Array.isArray(snapshot.quarantine?.items)
        ? snapshot.quarantine.items.filter(item => item?.reviewed !== true).length
        : null;
      let quarantineEmail = mailReady && Number.isInteger(quarantineCount) ? 'configured' : null;
      let syncEmail = mailReady ? 'configured' : null;
      const reports = Array.isArray(snapshot.sync_reports) ? snapshot.sync_reports : (snapshot.sync_report ? [snapshot.sync_report] : []);
      if (mailReady && reports.length) {
        const sentReport = await this.ctx.storage.get('sent_report');
        const lastIndex = sentReport ? reports.findIndex(report => report.id === sentReport) : -1;
        if (sentReport && lastIndex < 0) {
          syncEmail = 'error';
          console.error('Stockade stock update email history could not be matched.');
        } else {
          for (const report of reports.slice(lastIndex + 1)) {
            try {
              await sendEmail(this.env, 'Stockade stock update', report.text);
              await this.ctx.storage.put('sent_report', report.id);
              syncEmail = 'sent';
            } catch (_) {
              syncEmail = 'error';
              console.error('Stockade stock update email could not be sent.');
              break;
            }
          }
          if (lastIndex === reports.length - 1) syncEmail = 'sent';
        }
      }
      if (mailReady && Number.isInteger(quarantineCount)) {
        const alreadySent = await this.ctx.storage.get('quarantine_alert_sent');
        if (quarantineCount < 10) {
          await this.ctx.storage.put('quarantine_alert_sent', false);
        } else if (!alreadySent) {
          try {
            await sendEmail(this.env, 'Stockade quarantine alert', `The latest recorded quarantine snapshot contains ${quarantineCount} unreviewed items.`);
            await this.ctx.storage.put('quarantine_alert_sent', true);
            quarantineEmail = 'sent';
          } catch (_) {
            quarantineEmail = 'error';
            console.error('Stockade quarantine email could not be sent.');
          }
        } else {
          quarantineEmail = 'sent';
        }
      }
      const cycle = snapshot.last_cycle;
      let failureEmail = mailReady && cycle && Number.isInteger(cycle.consecutive_failures) ? 'configured' : null;
      if (mailReady && cycle && Number.isInteger(cycle.consecutive_failures)) {
        const alreadySent = await this.ctx.storage.get('sync_failure_alert_sent');
        if (cycle.result === 'ok') {
          await this.ctx.storage.put('sync_failure_alert_sent', false);
        } else if (cycle.result === 'failed' && cycle.consecutive_failures >= 4 && !alreadySent) {
          try {
            const detail = cycle.error ? `\n\n${cycle.error}` : '';
            await sendEmail(this.env, 'Stockade sync failure alert',
              `Stockade has had ${cycle.consecutive_failures} consecutive failed sync cycles.${detail}`);
            await this.ctx.storage.put('sync_failure_alert_sent', true);
            failureEmail = 'sent';
          } catch (_) {
            failureEmail = 'error';
            console.error('Stockade sync failure email could not be sent.');
          }
        } else if (cycle.result === 'failed' && alreadySent) {
          failureEmail = 'sent';
        }
      }
      const now = Date.now();
      const downtimeEmail = await this.observeHealth(snapshot, now);
      const { sync_reports, ...displaySnapshot } = snapshot;
      await this.ctx.storage.put('status', { received: now, snapshot: { ...displaySnapshot, quarantine_email: quarantineEmail, sync_email: syncEmail, failure_email: failureEmail, downtime_email: downtimeEmail } });
      await this.ctx.storage.setAlarm(now + 15 * 60 * 1000);
      const sentReport = await this.ctx.storage.get('sent_report');
      const responseHeaders = new Headers(headers);
      if (sentReport) responseHeaders.set('X-Stockade-Report-Id', sentReport);
      return new Response(null, { status: 204, headers: responseHeaders });
    }
    if (request.method === 'GET') {
      const stored = await this.ctx.storage.get('status');
      if (!stored) return Response.json({ service: 'unavailable', last_cycle: null, quarantine: null, sale_skips: null, connection: 'waiting' }, { headers });
      const current = Date.now() - stored.received <= 120000;
      return Response.json({ ...stored.snapshot, service: current ? stored.snapshot.service : 'unavailable', connection: current ? 'connected' : 'delayed' }, { headers });
    }
    return new Response('Method not allowed', { status: 405, headers });
  }
}

export default {
  async fetch(request, env) {
    if (!await accessAllowed(request, env)) return new Response('Access required', { status: 403, headers });
    const url = new URL(request.url);
    if (url.pathname === '/api/publish' && request.method === 'POST') {
      const provided = request.headers.get('X-Stockade-Bridge-Token') || '';
      if (!await sameSecret(provided, env.BRIDGE_TOKEN)) return new Response('Access required', { status: 403, headers });
      const store = env.SNAPSHOTS.get(env.SNAPSHOTS.idFromName('stockade'));
      return store.fetch(new Request('https://snapshot.internal/', { method: 'PUT', body: request.body, headers: { 'Content-Type': 'application/json' }, duplex: 'half' }));
    }
    if (url.pathname === '/api/status' && request.method === 'GET') {
      const store = env.SNAPSHOTS.get(env.SNAPSHOTS.idFromName('stockade'));
      return store.fetch('https://snapshot.internal/');
    }
    if (request.method === 'GET' && ['/', '/style.css', '/app.js'].includes(url.pathname)) {
      const asset = await env.ASSETS.fetch(request);
      const resultHeaders = new Headers(asset.headers);
      for (const [key, value] of Object.entries(headers)) resultHeaders.set(key, value);
      return new Response(asset.body, { status: asset.status, headers: resultHeaders });
    }
    return new Response('Not found', { status: 404, headers });
  },
};
