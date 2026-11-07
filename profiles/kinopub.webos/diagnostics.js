// Snapshot of kinopub.webos playback state for tools/devtools.py run.
// ES5 on purpose: the same file must run on older webOS engines.
(function () {
  function bufferAhead(video) {
    var t = video.currentTime;
    for (var i = 0; i < video.buffered.length; i++) {
      if (video.buffered.start(i) <= t + 0.05 && t <= video.buffered.end(i)) {
        return Math.round((video.buffered.end(i) - t) * 10) / 10;
      }
    }
    return 0;
  }

  function ranges(r) {
    var out = [];
    for (var i = 0; i < r.length; i++) out.push([Math.round(r.start(i) * 10) / 10, Math.round(r.end(i) * 10) / 10]);
    return out;
  }

  var video = document.querySelector('video');
  var state = null;
  if (video) {
    var quality = video.getVideoPlaybackQuality ? video.getVideoPlaybackQuality() : null;
    state = {
      currentTime: Math.round(video.currentTime * 10) / 10,
      duration: isFinite(video.duration) ? Math.round(video.duration) : String(video.duration),
      paused: video.paused,
      seeking: video.seeking,
      ended: video.ended,
      readyState: video.readyState,
      networkState: video.networkState,
      bufferAhead: bufferAhead(video),
      buffered: ranges(video.buffered),
      size: video.videoWidth + 'x' + video.videoHeight,
      error: video.error ? { code: video.error.code, message: video.error.message } : null,
      quality: quality ? { total: quality.totalVideoFrames, dropped: quality.droppedVideoFrames } : null,
      srcIsBlob: (video.currentSrc || '').indexOf('blob:') === 0
    };
  }

  // The overlay has no stable id; find it by its heading.
  var panel = null;
  var nodes = document.querySelectorAll('div');
  for (var i = 0; i < nodes.length; i++) {
    var n = nodes[i];
    if (n.childElementCount === 0 && n.textContent === 'Диагностика воспроизведения') {
      // The settings menu item has the same text; only the overlay sits inside a `ring` box.
      var box = n.parentElement;
      while (box && String(box.className).indexOf('ring') === -1) box = box.parentElement;
      if (box) {
        panel = box.innerText;
        break;
      }
    }
  }

  var active = document.activeElement;
  return {
    url: location.href,
    video: state,
    diagnosticsPanel: panel,
    focus: active ? (active.getAttribute('data-spotlight-id') || active.className || active.tagName) : null,
    at: new Date().toISOString()
  };
})()
