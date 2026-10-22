/* udisplayer: native video controls; bounded live recovery. MIT license. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const base = new URL('./', location.href);
  const pageUrl = new URL(location.href);
  const cid = pageUrl.searchParams.get('channel');
  const requestedGroup = pageUrl.searchParams.get('group');
  const validLogo = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  const updateUtc8Clock = () => {
    const now = new Date(Date.now() + 8 * 60 * 60 * 1000);
    const pad = value => String(value).padStart(2, '0');
    const time = `${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}:${pad(now.getUTCSeconds())}`;
    const date = `${now.getUTCFullYear()}/${pad(now.getUTCMonth() + 1)}/${pad(now.getUTCDate())} GMT+8`;
    $('utc8-clock-time').textContent = time;
    $('utc8-clock-date').textContent = date;
    $('utc8-clock').setAttribute('aria-label', `${time}, ${date}`);
  };
  updateUtc8Clock();
  setInterval(updateUtc8Clock, 1000);
  const createClientSession = () => {
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  };
  const prewarmSession = cid !== null && /^[1-9]\d*$/.test(cid) ? createClientSession() : null;
  let prewarmHandedOff = false;
  if (prewarmSession) {
    const prewarmUrl = new URL(`hls/${encodeURIComponent(cid)}/index.m3u8`, base);
    prewarmUrl.searchParams.set('session', prewarmSession);
    const prewarmReleaseUrl = new URL(`hls/${encodeURIComponent(cid)}/release`, base);
    prewarmReleaseUrl.searchParams.set('session', prewarmSession);
    // Register cleanup before starting prewarm. If the page is opened and closed
    // before channels.json resolves/showPlayer runs, the cold FFmpeg process must
    // not linger until the server idle timeout.
    window.addEventListener('pagehide', () => {
      if (!prewarmHandedOff) navigator.sendBeacon?.(prewarmReleaseUrl.href);
    }, {once: true});
    // Start the cold FFmpeg/HLS path in parallel with channels.json and UI setup.
    // HEAD produces no media body but exercises the same playlist startup path.
    fetch(prewarmUrl, {method: 'HEAD', cache: 'no-store'}).catch(() => {});
  }
  const notice = (element, text, error = false) => {
    if (element.textContent !== text) element.textContent = text;
    element.classList.toggle('error', error);
    element.hidden = false;
  };

  async function getChannels() {
    if (location.protocol === 'file:') throw new Error('請透過 HTTP 網站開啟此頁面。');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(new URL('channels.json', base), {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('無法讀取頻道列表，請確認網址與服務設定。');
      const data = await response.json();
      if (!data || !Array.isArray(data.channels) || typeof data.logo_version !== 'string' ||
          !/^[a-f0-9]{16}$/.test(data.logo_version)) throw new Error('頻道配置格式不正確。');
      if (data.groups !== undefined && (!Array.isArray(data.groups) || data.groups.some(group => typeof group !== 'string' || !group))) {
        throw new Error('頻道分組格式不正確。');
      }
      if (data.debug !== undefined && typeof data.debug !== 'boolean') throw new Error('頻道配置格式不正確。');
      const ids = new Set();
      for (const channel of data.channels) {
        if (!channel || !Number.isSafeInteger(channel.id) || channel.id < 1 || ids.has(channel.id) ||
            typeof channel.name !== 'string' || !channel.name.trim() ||
            (channel.group !== undefined && (typeof channel.group !== 'string' || !channel.group)) ||
            (channel.logo !== undefined && (typeof channel.logo !== 'string' || !validLogo.test(channel.logo)))) {
          throw new Error('頻道配置包含無效或重複的項目。');
        }
        ids.add(channel.id);
      }
      return data;
    } finally { clearTimeout(timer); }
  }

  function showList(data) {
    const channels = data.channels;
    const groups = Array.isArray(data.groups) ? data.groups : [];
    $('list-view').hidden = false;
    $('channel-count').textContent = `${channels.length} 個頻道`;
    channels.forEach(channel => {
      const item = document.createElement('li');
      const link = document.createElement('a');
      const url = new URL(base);
      url.searchParams.set('channel', channel.id);
      link.href = url.href;
      link.className = 'channel-card';
      link.setAttribute('aria-label', `${channel.name}，播放頻道`);
      item.dataset.group = channel.group || '';
      const logo = document.createElement('span');
      logo.className = 'channel-logo';
      logo.textContent = '▶';
      logo.setAttribute('aria-hidden', 'true');
      if (channel.logo) {
        const image = document.createElement('img');
        const logoUrl = new URL(`logos/${encodeURIComponent(channel.logo)}`, base);
        logoUrl.searchParams.set('v', data.logo_version);
        image.src = logoUrl.href;
        image.alt = '';
        image.loading = 'lazy';
        image.decoding = 'async';
        image.addEventListener('error', () => { image.remove(); logo.textContent = '▶'; }, {once: true});
        logo.textContent = '';
        logo.append(image);
      }
      link.append(logo);
      for (const [cls, text] of [['channel-name', channel.name], ['channel-arrow', '→']]) {
        const span = document.createElement('span');
        span.className = cls;
        span.textContent = text;
        if (cls !== 'channel-name') span.setAttribute('aria-hidden', 'true');
        link.append(span);
      }
      item.append(link);
      $('channels').append(item);
    });
    if (groups.length) {
      const groupNav = $('channel-groups');
      const filters = [['', '全部'], ...groups.map(group => [group, group])];
      const applyGroup = selected => {
        let visible = 0;
        for (const item of $('channels').children) {
          item.hidden = Boolean(selected) && item.dataset.group !== selected;
          if (!item.hidden) visible++;
          const channelLink = item.querySelector('.channel-card');
          const channelUrl = new URL(channelLink.href);
          if (selected) channelUrl.searchParams.set('group', selected);
          else channelUrl.searchParams.delete('group');
          channelLink.href = channelUrl.href;
        }
        $('channel-count').textContent = `${visible} 個頻道`;
        for (const button of groupNav.querySelectorAll('.group-filter')) {
          const active = button.dataset.group === selected;
          button.classList.toggle('active', active);
          button.setAttribute('aria-pressed', String(active));
        }
        const listUrl = new URL(base);
        if (selected) listUrl.searchParams.set('group', selected);
        history.replaceState(null, '', listUrl.href);
      };
      for (const [group, label] of filters) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'group-filter';
        button.dataset.group = group;
        button.textContent = label;
        button.addEventListener('click', () => applyGroup(group));
        groupNav.append(button);
      }
      groupNav.hidden = false;
      applyGroup(requestedGroup && groups.includes(requestedGroup) ? requestedGroup : '');
    }
    if (!channels.length) notice($('list-message'), '尚未設定頻道。');
    // Geometric navigation follows the actual responsive grid, including TV layouts.
    $('channels').addEventListener('keydown', event => {
      const links = [...document.querySelectorAll('.channel-card')].filter(link => !link.closest('li').hidden);
      const current = links.indexOf(document.activeElement);
      if (current < 0 || !['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      const origin = links[current].getBoundingClientRect();
      const vertical = ['ArrowUp', 'ArrowDown'].includes(event.key);
      const forward = ['ArrowDown', 'ArrowRight'].includes(event.key);
      let best = null, distance = Infinity;
      for (const link of links) {
        if (link === links[current]) continue;
        const r = link.getBoundingClientRect();
        const dx = r.left - origin.left, dy = r.top - origin.top;
        const primary = vertical ? dy : dx, secondary = vertical ? dx : dy;
        if ((forward ? primary > 5 : primary < -5) && Math.abs(primary) + Math.abs(secondary) * 3 < distance) {
          best = link; distance = Math.abs(primary) + Math.abs(secondary) * 3;
        }
      }
      event.preventDefault();
      if (best) best.focus();
    });
  }

  function showPlayer(channel, selectedGroup, debugEnabled = false, clientSession = createClientSession()) {
    $('player-view').hidden = false;
    const backUrl = new URL(base);
    if (selectedGroup) backUrl.searchParams.set('group', selectedGroup);
    $('player-view').querySelector('.back-link').href = backUrl.href;
    $('channel-title').textContent = channel.name;
    document.title = `${channel.name} · IPTV Player`;
    const video = $('video'), status = $('playback-status');
    const source = new URL(`hls/${encodeURIComponent(channel.id)}/index.m3u8`, base);
    source.searchParams.set('session', clientSession);
    const releaseUrl = new URL(`hls/${encodeURIComponent(channel.id)}/release`, base);
    releaseUrl.searchParams.set('session', clientSession);
    const telemetryUrl = new URL('telemetry', base);
    const nativeSupport = Boolean(video.canPlayType('application/vnd.apple.mpegurl'));
    // Prefer hls.js whenever Media Source / Managed Media Source is available.
    // Modern iOS can then use the same bounded buffering and recovery policy as
    // desktop browsers instead of bypassing it through native AVFoundation HLS.
    const hlsSupported = Boolean(window.Hls && Hls.isSupported());
    const native = !hlsSupported && nativeSupport;
    const state = {
      hls: null, generation: 0, intent: true, blocked: false, terminal: false, disposed: false,
      rebuilding: false, timer: null, due: 0, attempt: 0, ready: false, lastTime: 0,
      lastProgress: performance.now(), startedAt: 0, stableSince: 0, waitingSince: 0,
      lastEdge: 0, edgeAdvancedAt: 0, lagTicks: 0, lastSeek: 0, lastDrop: 0,
      cap: -1, pausedAt: 0, hiddenAt: 0, recoveries: 0, liveJumps: 0, stalls: 0,
      phase: 'loading', lastReason: '', lastHlsError: '', lastFragMs: null,
      lastFragBytes: null, lastFragKbps: null, normalPlayingSent: false
    };
    const visible = () => !document.hidden || document.pictureInPictureElement === video || video.webkitDisplayingFullscreen;
    const liveRange = () => video.seekable.length ? video.seekable : video.buffered;
    const finite = value => Number.isFinite(value) ? Math.round(value * 1000) / 1000 : null;
    const bufferAhead = () => {
      const current = video.currentTime;
      for (let i = 0; i < video.buffered.length; i++) {
        if (current >= video.buffered.start(i) - 0.05 && current <= video.buffered.end(i) + 0.05) {
          return Math.max(0, video.buffered.end(i) - current);
        }
      }
      return 0;
    };
    const edge = () => {
      const ranges = liveRange();
      if (!ranges.length) return null;
      return ranges.end(ranges.length - 1);
    };
    const setStatus = (message, error = false) => notice(status, message, error);
    function telemetryPayload(event, extra = {}) {
      const latest = edge();
      const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
      return {
        event,
        channel: channel.id,
        session: clientSession,
        engine: hlsSupported ? 'hls.js' : (native ? 'native' : 'none'),
        phase: state.phase,
        current_time: finite(video.currentTime),
        edge: finite(latest),
        live_lag_s: latest === null ? null : finite(Math.max(0, latest - video.currentTime)),
        buffer_s: finite(bufferAhead()),
        recoveries: state.recoveries,
        stalls: state.stalls,
        live_jumps: state.liveJumps,
        retry: state.attempt,
        current_level: state.hls?.currentLevel ?? null,
        cap: state.cap,
        frag_ms: state.lastFragMs,
        frag_bytes: state.lastFragBytes,
        frag_kbps: state.lastFragKbps,
        online: navigator.onLine,
        visible: visible(),
        effective_type: connection?.effectiveType || null,
        downlink_mbps: finite(connection?.downlink),
        rtt_ms: finite(connection?.rtt),
        save_data: Boolean(connection?.saveData),
        ...extra
      };
    }
    function sendTelemetry(event, extra = {}, beacon = false) {
      const normalEvents = new Set(['session_start', 'playing', 'recovery', 'terminal', 'session_end']);
      if (!debugEnabled && !normalEvents.has(event)) return;
      if (!debugEnabled && event === 'playing' && state.normalPlayingSent) return;
      if (event === 'playing') state.normalPlayingSent = true;
      const full = telemetryPayload(event, extra);
      const payload = debugEnabled ? full : {
        event: full.event,
        channel: full.channel,
        session: full.session,
        engine: full.engine,
        recoveries: full.recoveries,
        stalls: full.stalls,
        live_jumps: full.live_jumps,
        reason: extra.reason ?? null,
        hls_error: extra.hls_error ?? null
      };
      const body = JSON.stringify(payload);
      if (beacon && navigator.sendBeacon) {
        navigator.sendBeacon(telemetryUrl.href, body);
        return;
      }
      fetch(telemetryUrl, {
        method: 'POST',
        body,
        cache: 'no-store',
        keepalive: true,
        headers: {'Content-Type': 'application/json'}
      }).catch(() => {});
    }
    const terminal = message => {
      state.terminal = true;
      state.phase = 'error';
      state.lastReason = message;
      clearTimeout(state.timer); state.timer = null;
      state.hls?.stopLoad();
      sendTelemetry('terminal', {reason: message, hls_error: state.lastHlsError || null});
      setStatus('發生錯誤請稍後再試', true);
    };
    function attemptPlay(generation) {
      if (!state.intent || state.blocked || state.disposed) return;
      const promise = video.play();
      if (promise) promise.catch(error => {
        if (generation !== state.generation || state.disposed) return;
        if (error.name === 'NotAllowedError') {
          state.blocked = true;
          state.rebuilding = false;
          state.phase = 'awaiting-play';
          setStatus('暫停中  按 ► 開始直播');
        } else if (error.name !== 'AbortError') scheduleRecovery('播放暫時中斷');
      });
    }
    function jumpLive(force = false) {
      const latest = edge();
      if (latest === null || !Number.isFinite(latest)) return false;
      const ranges = liveRange();
      const start = ranges.start(ranges.length - 1);
      const target = Math.max(start + 0.05, latest - 6);
      if (target >= latest || (!force && latest - video.currentTime <= 15)) return false;
      try {
        video.currentTime = target;
        state.liveJumps++;
        state.lastSeek = performance.now();
        state.lagTicks = 0;
        sendTelemetry('live_jump', {target_time: finite(target)});
        return true;
      } catch { return false; }
    }
    function lowerQuality() {
      const hls = state.hls, now = performance.now();
      if (!hls || now - state.lastDrop < 5000) return;
      const current = Math.max(0, hls.currentLevel >= 0 ? hls.currentLevel : hls.nextAutoLevel);
      state.cap = Math.max(0, Math.min(state.cap < 0 ? current : state.cap, current) - 1);
      hls.autoLevelCapping = state.cap;
      state.lastDrop = now;
      state.stableSince = 0;
    }
    function scheduleRecovery(reason, immediate = false) {
      if (state.disposed || state.terminal || state.blocked || !state.intent) return;
      state.lastReason = reason;
      if (state.timer !== null && performance.now() < state.due && !immediate) return;
      clearTimeout(state.timer);
      state.hls?.stopLoad();
      state.phase = 'recovering';
      state.stableSince = 0;
      sendTelemetry('recovery', {reason, immediate});
      setStatus('正在加載');
      const steps = [1000, 2000, 4000, 8000, 15000];
      const delay = immediate ? 0 : steps[Math.min(state.attempt++, steps.length - 1)] * (0.9 + Math.random() * 0.2);
      state.due = performance.now() + delay;
      state.timer = setTimeout(() => {
        state.timer = null;
        if (visible()) connect(true);
        // Background browser timers may be suspended. Visibility resume checks the deadline.
      }, delay);
    }
    function connect(recovery = false) {
      if (state.disposed || state.terminal || !state.intent) return;
      const generation = ++state.generation;
      state.rebuilding = true;
      state.ready = false;
      state.startedAt = state.lastProgress = performance.now();
      state.lastTime = 0; state.lastEdge = 0; state.edgeAdvancedAt = 0;
      state.waitingSince = 0; state.stableSince = 0; state.lagTicks = 0;
      state.phase = recovery ? 'recovering' : 'loading';
      if (recovery) state.recoveries++;
      state.hls?.destroy(); state.hls = null;
      video.removeAttribute('src');
      video.load();
      const url = new URL(source);
      if (hlsSupported) {
        const retry = {maxNumRetry: 1, retryDelayMs: 500, maxRetryDelayMs: 1000};
        const policy = (first, total) => ({default: {maxTimeToFirstByteMs: first, maxLoadTimeMs: total,
          timeoutRetry: {...retry}, errorRetry: {...retry}}});
        const hls = new Hls({
          preferManagedMediaSource: true,
          lowLatencyMode: false, initialLiveManifestSize: 1,
          liveSyncDuration: 6, liveMaxLatencyDuration: 15,
          liveSyncOnStallIncrease: 0, maxLiveSyncPlaybackRate: 1,
          maxBufferLength: 30, maxMaxBufferLength: 45, backBufferLength: 15,
          frontBufferFlushThreshold: 40, startLevel: 0, capLevelToPlayerSize: true,
          manifestLoadPolicy: policy(35000, 45000), playlistLoadPolicy: policy(35000, 45000),
          fragLoadPolicy: policy(12000, 30000), liveMaxUnchangedPlaylistRefresh: 10
        });
        state.hls = hls;
        hls.on(Hls.Events.MEDIA_ATTACHED, () => {
          if (generation === state.generation) hls.loadSource(url.href);
        });
        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          if (generation !== state.generation) return;
          if (state.cap >= 0) hls.autoLevelCapping = Math.min(state.cap, hls.levels.length - 1);
          attemptPlay(generation);
        });
        hls.on(Hls.Events.FRAG_LOADED, (_event, data) => {
          if (generation !== state.generation || state.disposed) return;
          const loaded = data.stats?.loaded;
          const start = data.stats?.loading?.start;
          const end = data.stats?.loading?.end;
          if (Number.isFinite(loaded) && Number.isFinite(start) && Number.isFinite(end) && end > start) {
            const duration = end - start;
            state.lastFragMs = Math.round(duration);
            state.lastFragBytes = loaded;
            state.lastFragKbps = finite(loaded * 8 / duration);
          }
        });
        hls.on(Hls.Events.ERROR, (_event, data) => {
          if (generation !== state.generation || state.disposed) return;
          state.lastHlsError = `${data.details || data.type || 'unknown'}${data.response?.code ? ` (${data.response.code})` : ''}`;
          const code = data.response?.code;
          sendTelemetry('hls_error', {
            hls_error: state.lastHlsError,
            fatal: Boolean(data.fatal),
            http_status: code ?? null
          });
          if ([401, 403].includes(code) || (code === 404 && data.details === Hls.ErrorDetails.MANIFEST_LOAD_ERROR)) {
            terminal('此頻道或網址已無法使用，請返回列表或確認服務設定。');
          } else if (data.details === Hls.ErrorDetails.MANIFEST_INCOMPATIBLE_CODECS_ERROR) {
            terminal('此瀏覽器無法解碼影片，請使用支援 H.264／AAC 的瀏覽器。');
          } else if (data.fatal) {
            lowerQuality();
            scheduleRecovery('直播連線暫時中斷');
          } else if (data.details === Hls.ErrorDetails.BUFFER_STALLED_ERROR) lowerQuality();
        });
        hls.attachMedia(video);
      } else if (native) {
        video.src = url.href;
        attemptPlay(generation);
      } else terminal('此瀏覽器不支援 HLS 直播播放。');
    }
    video.addEventListener('loadedmetadata', () => {
      state.ready = true;
      state.lastProgress = performance.now();
      attemptPlay(state.generation);
    });
    video.addEventListener('playing', () => {
      const now = performance.now();
      const stallMs = state.waitingSince ? Math.round(now - state.waitingSince) : 0;
      state.rebuilding = false; state.blocked = false;
      state.waitingSince = 0; state.lastProgress = now;
      state.stableSince ||= now;
      state.phase = 'playing';
      if (stallMs) {
        // Keep the detailed stall event in debug telemetry, but always emit the
        // canonical playing event as well. Normal mode suppresses stall_end, and
        // the server uses playing to record successful startup exactly once.
        sendTelemetry('stall_end', {stall_ms: stallMs});
        sendTelemetry('playing');
      } else sendTelemetry('playing');
      setStatus('直播中');
    });
    video.addEventListener('play', () => {
      const wasPaused = !state.intent;
      state.intent = true; state.blocked = false;
      if (wasPaused) {
        if (performance.now() - state.pausedAt > 10000) scheduleRecovery('正在接上直播', true);
        else {
          jumpLive(true);
          state.hls?.startLoad(-1);
        }
      }
    });
    video.addEventListener('pause', () => {
      // Safari may emit pause after play() is rejected by autoplay policy. That
      // is not a user pause: keep the awaiting-play prompt until the user starts playback.
      if (state.disposed || state.rebuilding || state.blocked || video.error || video.ended || !visible()) return;
      state.intent = false; state.pausedAt = performance.now(); state.phase = 'paused';
      clearTimeout(state.timer); state.timer = null;
      state.hls?.stopLoad();
      sendTelemetry('paused');
      setStatus('暫停中  按 ► 開始直播');
    });
    video.addEventListener('waiting', () => {
      if (!state.intent || state.blocked) return;
      if (!state.waitingSince) {
        state.waitingSince = performance.now();
        state.stalls++;
        sendTelemetry('stall_start');
      }
      state.stableSince = 0;
      setStatus('正在加載');
    });
    video.addEventListener('error', () => {
      if (!native || state.disposed) return;
      // Native engines often report a network/HTTP failure as SRC_NOT_SUPPORTED.
      // Check the HTTP endpoint before treating that opaque native error as terminal.
      inspectNativeFailure();
    });
    let probeController = null;
    async function inspectNativeFailure() {
      if (probeController || !state.intent || state.disposed) return;
      probeController = new AbortController();
      const generation = state.generation;
      const timeout = setTimeout(() => probeController?.abort(), 35000);
      try {
        const response = await fetch(source, {method: 'HEAD', cache: 'no-store', signal: probeController.signal});
        if (generation !== state.generation || state.disposed) return;
        if ([401, 403, 404].includes(response.status)) terminal('此頻道或網址已無法使用，請返回列表。');
        else if (response.ok && video.error && [3, 4].includes(video.error.code)) terminal('裝置無法解碼此影片，請確認 FFmpeg 輸出與瀏覽器支援。');
        else scheduleRecovery('直播連線暫時中斷');
      } catch { if (!state.disposed) scheduleRecovery('直播連線暫時中斷'); }
      finally { clearTimeout(timeout); probeController = null; }
    }
    function tick() {
      if (!visible() || state.disposed || state.terminal || state.blocked || !state.intent) return;
      const now = performance.now();
      if (state.timer !== null) {
        if (now > state.due + 1000) scheduleRecovery('正在恢復連線', true);
        return;
      }
      if (state.phase === 'recovering' && !state.rebuilding) { scheduleRecovery('正在恢復連線', true); return; }
      if (!state.ready) {
        if (now - state.startedAt > 35000) scheduleRecovery('串流啟動逾時');
        return;
      }
      if (Math.abs(video.currentTime - state.lastTime) > 0.05) {
        state.lastTime = video.currentTime; state.lastProgress = now;
      }
      if (state.waitingSince && now - state.waitingSince > 2000) lowerQuality();
      if (!video.seeking && now - state.lastProgress > 15000) { scheduleRecovery('播放暫時停頓'); return; }
      const latest = edge();
      if (latest !== null) {
        if (Math.abs(latest - state.lastEdge) > 0.1) { state.lastEdge = latest; state.edgeAdvancedAt = now; }
        if (state.edgeAdvancedAt && now - state.edgeAdvancedAt > 30000) { scheduleRecovery('正在取得最新直播'); return; }
        state.lagTicks = latest - video.currentTime > 15 ? state.lagTicks + 1 : 0;
        if (state.lagTicks >= 2 && now - state.lastSeek > 5000) {
          lowerQuality();
          if (state.hls) state.hls.stopLoad();
          const jumped = jumpLive();
          if (state.hls) state.hls.startLoad(jumped ? video.currentTime : -1);
          if (!jumped) scheduleRecovery('正在接上直播');
        }
      }
      if (state.stableSince && now - state.stableSince > 30000) {
        state.attempt = 0;
        if (state.hls && state.cap >= 0 && now - state.lastDrop > 30000) {
          state.cap = state.cap + 1 >= state.hls.levels.length - 1 ? -1 : state.cap + 1;
          state.hls.autoLevelCapping = state.cap;
          state.lastDrop = now;
        }
      }
    }
    const interval = setInterval(tick, 1000);
    const telemetryInterval = debugEnabled ? setInterval(() => {
      if (!state.disposed) sendTelemetry('heartbeat');
    }, 15000) : null;
    function resumeCheck() {
      if (!visible() || !state.intent || state.blocked || state.terminal) return;
      if (state.hiddenAt && performance.now() - state.hiddenAt > 10000) scheduleRecovery('正在接上直播', true);
      else if (state.phase === 'recovering' || (state.timer !== null && performance.now() >= state.due)) scheduleRecovery('正在恢復連線', true);
      else tick();
      state.hiddenAt = 0;
    }
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) state.hiddenAt = performance.now();
      else resumeCheck();
    });
    window.addEventListener('offline', () => {
      sendTelemetry('offline');
      if (state.intent && !state.blocked && !state.terminal) setStatus('正在加載');
    });
    window.addEventListener('online', () => {
      sendTelemetry('online');
      if (state.intent && !state.blocked) scheduleRecovery('網路已恢復', true);
    });
    window.addEventListener('pagehide', () => {
      sendTelemetry('session_end', {reason: 'pagehide'}, true);
      state.disposed = true; ++state.generation;
      clearInterval(interval); if (telemetryInterval) clearInterval(telemetryInterval); clearTimeout(state.timer);
      probeController?.abort(); state.hls?.destroy(); state.hls = null;
      video.removeAttribute('src'); video.load();
      // Best-effort immediate release for normal navigation/tab/window close.
      // If the browser process or network disappears before delivery, the
      // server-side idle timeout remains the fallback cleanup path.
      navigator.sendBeacon?.(releaseUrl.href);
    });
    window.addEventListener('pageshow', event => {
      if (event.persisted) location.reload(); else resumeCheck();
    });
    // Read-only diagnostics remain available for manual/browser acceptance.
    // The player now owns this prewarm session and has its own pagehide
    // release handler above; suppress the early prewarm-only fallback.
    if (clientSession === prewarmSession) prewarmHandedOff = true;
    Object.defineProperty(window, '__udisplayerDebug', {get: () => ({
      native, engine: hlsSupported ? 'hls.js' : (native ? 'native' : 'none'),
      phase: state.phase, intent: state.intent, blocked: state.blocked,
      recoveries: state.recoveries, stalls: state.stalls, liveJumps: state.liveJumps, retry: state.attempt,
      currentTime: video.currentTime, edge: edge(), currentLevel: state.hls?.currentLevel ?? null,
      levels: state.hls?.levels.map(l => ({width: l.width, height: l.height, bitrate: l.bitrate})) ?? [],
      cap: state.cap, lastReason: state.lastReason, lastHlsError: state.lastHlsError,
      fragMs: state.lastFragMs, fragBytes: state.lastFragBytes, fragKbps: state.lastFragKbps,
      buffered: Array.from({length: video.buffered.length}, (_, i) => [video.buffered.start(i), video.buffered.end(i)])
    })});
    sendTelemetry('session_start');
    connect();
  }

  getChannels().then(data => {
    $('global-status').hidden = true;
    if (cid !== null) {
      const numericId = Number(cid);
      const channel = data.channels.find(c => c.id === numericId);
      if (!channel) throw new Error('Not found');
      const selectedGroup = requestedGroup && Array.isArray(data.groups) && data.groups.includes(requestedGroup)
        ? requestedGroup : '';
      showPlayer(channel, selectedGroup, Boolean(data.debug), prewarmSession || undefined);
    } else showList(data);
  }).catch(error => notice($('global-status'), error.name === 'AbortError'
    ? '讀取頻道逾時，請確認網路後重新整理。' : error.message, true));
})();
