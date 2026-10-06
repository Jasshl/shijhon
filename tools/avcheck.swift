// Headless AVFoundation playback check (macOS): what an AVPlayer-based client does
// with a stream URL. Plays muted, measures time to start, seeks to the middle
// and checks playback continues there, then downloads the file and opens it as an asset.
//
//   swiftc -O tools/avcheck.swift -o avcheck
//   ./avcheck --stream '<stream URL>' [--download '<download URL>'] [--mime audio/flac]
//
// Prints one JSON object. Exit status 0 when every requested step passed.

import AVFoundation
import Foundation

struct Options {
    var stream: URL?
    var download: URL?
    var mime: String?
    var timeout: Double = 30
}

func parse() -> Options {
    var options = Options()
    var args = CommandLine.arguments.dropFirst().makeIterator()
    while let arg = args.next() {
        switch arg {
        case "--stream": options.stream = args.next().flatMap(URL.init(string:))
        case "--download": options.download = args.next().flatMap(URL.init(string:))
        case "--mime": options.mime = args.next()
        case "--timeout": options.timeout = Double(args.next() ?? "") ?? 30
        default: break
        }
    }
    return options
}

func now() -> Double { Date().timeIntervalSince1970 }

@MainActor
func waitUntil(_ timeout: Double, _ condition: () -> Bool) async -> Bool {
    let deadline = now() + timeout
    while now() < deadline {
        if condition() { return true }
        try? await Task.sleep(nanoseconds: 50_000_000)
    }
    return condition()
}

@MainActor
func checkPlayback(_ url: URL, mime: String?, timeout: Double) async -> [String: Any] {
    var result: [String: Any] = [:]
    var assetOptions: [String: Any] = [:]
    if let mime { assetOptions["AVURLAssetOutOfBandMIMETypeKey"] = mime }
    let asset = AVURLAsset(url: url, options: assetOptions)
    let item = AVPlayerItem(asset: asset)
    let player = AVPlayer(playerItem: item)
    player.isMuted = true
    player.automaticallyWaitsToMinimizeStalling = true
    let started = now()
    player.play()
    let playing = await waitUntil(timeout) {
        item.status == .failed || player.currentTime().seconds >= 1.0
    }
    if item.status == .failed || !playing {
        result["play"] = false
        result["error"] = item.error.map { "\($0.localizedDescription)" } ?? "timed out"
        return result
    }
    result["play"] = true
    result["start_seconds"] = ((now() - started) * 1000).rounded() / 1000
    let duration = item.duration.seconds
    result["duration"] = duration.isFinite ? duration : -1
    guard duration.isFinite, duration > 4 else {
        result["seek"] = false
        result["error"] = "unknown duration"
        return result
    }
    let target = CMTime(seconds: duration / 2, preferredTimescale: 600)
    let seekStarted = now()
    let finished = await withCheckedContinuation { continuation in
        player.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero) { done in
            continuation.resume(returning: done)
        }
    }
    let advanced = await waitUntil(timeout) {
        player.currentTime().seconds >= duration / 2 + 0.5
    }
    result["seek"] = finished && advanced
    result["seek_seconds"] = ((now() - seekStarted) * 1000).rounded() / 1000
    player.pause()
    return result
}

func checkDownload(_ url: URL, timeout: Double) async -> [String: Any] {
    var result: [String: Any] = [:]
    let started = now()
    let config = URLSessionConfiguration.ephemeral
    config.timeoutIntervalForResource = timeout
    do {
        let (file, response) = try await URLSession(configuration: config).download(from: url)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let type = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Type") ?? ""
        let suffix = type.contains("flac") ? "flac" : type.contains("mp4") ? "m4a" : "mp3"
        let local = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString + "." + suffix)
        try FileManager.default.moveItem(at: file, to: local)
        defer { try? FileManager.default.removeItem(at: local) }
        let size = (try? FileManager.default.attributesOfItem(atPath: local.path)[.size] as? Int) ?? 0
        let asset = AVURLAsset(url: local)
        let playable = try await asset.load(.isPlayable)
        let duration = try await asset.load(.duration).seconds
        result["download"] = status == 200 && playable && duration > 1
        result["download_status"] = status
        result["download_type"] = type
        result["download_bytes"] = size
        result["download_duration"] = duration
        result["download_seconds"] = ((now() - started) * 1000).rounded() / 1000
    } catch {
        result["download"] = false
        result["error"] = "\(error.localizedDescription)"
    }
    return result
}

@main
struct AVCheck {
    static func main() async {
        let options = parse()
        var report: [String: Any] = [:]
        var ok = true
        if let url = options.stream {
            let playback = await checkPlayback(url, mime: options.mime, timeout: options.timeout)
            report.merge(playback) { $1 }
            ok = ok && (playback["play"] as? Bool ?? false) && (playback["seek"] as? Bool ?? false)
        }
        if let url = options.download {
            let download = await checkDownload(url, timeout: options.timeout)
            report.merge(download) { $1 }
            ok = ok && (download["download"] as? Bool ?? false)
        }
        let data = try? JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
        print(String(data: data ?? Data("{}".utf8), encoding: .utf8) ?? "{}")
        exit(ok ? 0 : 1)
    }
}
