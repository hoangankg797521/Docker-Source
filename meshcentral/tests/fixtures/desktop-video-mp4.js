// Record the rendered remote desktop as a standard H.264 MP4, not KVM packets.
var deskVideoRecording = null;
var deskVideoLast = null;

function deskVideoPreview() {
    if (!deskVideoLast) return false;
    var html = '<video id="DeskVideoPreview" controls playsinline preload="metadata" style="width:100%;max-height:60vh;background:#000" src="' + deskVideoLast.url + '"></video>';
    html += '<p>' + EscapeHtml(deskVideoLast.filename) + '</p>';
    setDialogMode(2, 'Video MP4', 2, null, html);
    return false;
}

function deskVideoPublish(blob, filename) {
    if (deskVideoLast) URL.revokeObjectURL(deskVideoLast.url);
    deskVideoLast = { url: URL.createObjectURL(blob), filename: filename };
    var button = Q('DeskRecordButton'), link = Q('DeskVideoDownload'), preview = Q('DeskVideoOpen');
    if (!link) {
        link = document.createElement('a'); link.id = 'DeskVideoDownload';
        link.textContent = 'Tải MP4'; link.style.cssText = 'margin-left:6px;font-size:11px';
        button.appendChild(link);
        preview = document.createElement('a'); preview.id = 'DeskVideoOpen';
        preview.textContent = 'Xem video'; preview.href = '#'; preview.onclick = deskVideoPreview;
        preview.style.cssText = 'margin-left:6px;font-size:11px'; button.appendChild(preview);
    }
    link.href = deskVideoLast.url; link.download = filename;
    // Keep an explicit download/preview available if the browser blocks auto-download.
    saveAs(blob, filename);
}

function deskVideoFilename(name, date) {
    return 'Desktop-' + String(name || 'Client').replace(/[\\/:*?"<>|\x00-\x1f]/g, '_') + '-' +
        date.getFullYear() + '-' + ('0' + (date.getMonth() + 1)).slice(-2) + '-' + ('0' + date.getDate()).slice(-2) + '-' +
        ('0' + date.getHours()).slice(-2) + '-' + ('0' + date.getMinutes()).slice(-2) + '-' + ('0' + date.getSeconds()).slice(-2) + '.mp4';
}

function deskVideoIndicator(recording) {
    var image = Q('DeskRecordButtonImage'), button = Q('DeskRecordButton');
    if (!image || !button) return;
    image.src = recording ? 'images/icon-film-red.png' : 'images/icon-film.png';
    button.title = recording ? 'Dừng quay và tải video MP4' : 'Quay màn hình thành video MP4';
    var label = Q('DeskVideoLabel');
    if (!label) {
        label = document.createElement('span'); label.id = 'DeskVideoLabel';
        label.style.cssText = 'font-size:11px;margin-left:3px;vertical-align:top;cursor:pointer';
        label.onclick = deskRecordSession;
        button.appendChild(label);
    }
    label.textContent = recording ? '● MP4' : 'MP4';
    label.style.color = recording ? '#d71920' : '';
}

function deskVideoStop() {
    var recording = deskVideoRecording;
    if (!recording || recording.stopping) return;
    recording.stopping = true;
    clearInterval(recording.timer);
    deskVideoIndicator(false);
    if (recording.recorder.state !== 'inactive') recording.recorder.stop();
    else recording.finish();
}

function deskRecordSession() {
    if (deskVideoRecording) { deskVideoStop(); return; }
    if (!desktop || desktop.State !== 3 || (features2 & 0x400)) return;
    var source = Q('Desk');
    if (!source || !source.width || !source.height || typeof MediaRecorder === 'undefined' || !source.captureStream) {
        messagebox('Quay video MP4', 'Trình duyệt chưa hỗ trợ quay video. Hãy dùng Chrome hoặc Edge phiên bản mới.'); return;
    }
    // Explicit H.264 excludes MP4 codecs that stock Windows/macOS players may lack.
    var mimeTypes = ['video/mp4;codecs=avc1.42E028', 'video/mp4;codecs=avc1.420028', 'video/mp4;codecs=avc1'];
    var mime = mimeTypes.filter(function(type) { return MediaRecorder.isTypeSupported(type); })[0];
    if (!mime) {
        messagebox('Quay video MP4', 'Trình duyệt này chưa hỗ trợ mã hóa MP4/H.264. Hãy mở MeshCentral bằng Chrome hoặc Edge phiên bản mới để quay MP4.'); return;
    }
    var output = document.createElement('canvas');
    var scale = Math.min(1, 1920 / source.width, 1080 / source.height);
    output.width = Math.max(2, Math.floor(source.width * scale / 2) * 2);
    output.height = Math.max(2, Math.floor(source.height * scale / 2) * 2);
    var context = output.getContext('2d', { alpha: false });
    var stream, recorder;
    function draw() {
        if (!source.width || !source.height) return;
        var fit = Math.min(output.width / source.width, output.height / source.height);
        var w = source.width * fit, h = source.height * fit;
        context.fillStyle = '#000'; context.fillRect(0, 0, output.width, output.height);
        context.drawImage(source, (output.width - w) / 2, (output.height - h) / 2, w, h);
    }
    try {
        draw(); stream = output.captureStream(15);
        recorder = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: 4000000 });
    } catch (error) {
        if (stream) stream.getTracks().forEach(function(track) { track.stop(); });
        messagebox('Quay video MP4', 'Không thể khởi tạo bộ mã hóa MP4: ' + error.message); return;
    }
    var recording = {
        recorder: recorder, stream: stream, chunks: [], stopping: false, finished: false, error: null,
        filename: deskVideoFilename(currentNode.name, new Date())
    };
    recording.finish = function() {
        if (recording.finished) return;
        recording.finished = true;
        clearInterval(recording.timer);
        stream.getTracks().forEach(function(track) { track.stop(); });
        if (deskVideoRecording === recording) { deskVideoRecording = null; deskVideoIndicator(false); }
        if (!recording.error && recording.chunks.length) {
            deskVideoPublish(new Blob(recording.chunks, { type: recorder.mimeType || mime }), recording.filename);
        } else {
            messagebox('Quay video MP4', recording.error ? 'Quay video bị lỗi: ' + recording.error : 'Chưa nhận được khung hình video. Hãy thử quay lại trong vài giây.');
        }
        recording.chunks = [];
    };
    recorder.ondataavailable = function(event) { if (event.data && event.data.size) recording.chunks.push(event.data); };
    recorder.onstop = recording.finish;
    recorder.onerror = function(event) {
        recording.error = event.error ? event.error.message : 'Bộ mã hóa dừng bất thường';
        deskVideoStop();
    };
    deskVideoRecording = recording;
    try {
        // Redraw at a fixed cadence even for a static desktop; dimensions remain stable
        // when the remote resolution/monitor changes during a recording.
        recording.timer = setInterval(draw, 1000 / 15);
        recorder.start(1000);
        deskVideoIndicator(true);
    } catch (error) {
        recording.error = error.message;
        recording.finish();
    }
}

window.addEventListener('beforeunload', function(event) {
    if (!deskVideoRecording) return;
    event.preventDefault(); event.returnValue = '';
});
