// meeting-recorder — record one app's audio and the microphone (macOS 15+).
//
//   meeting-recorder <out-dir> [bundle-id]      (default com.google.Chrome)
//
// ScreenCaptureKit captures only the named app's audio (other apps and
// notification sounds stay out) and, from macOS 15, the microphone. Each goes
// to its own AAC file in <out-dir>: app.m4a (the other people in the meeting)
// and mic.m4a (you). SIGINT or SIGTERM ends the recording and finishes both
// files; the server mixes them (runtime/meeting_recorder.py).
//
// The first run asks for Screen & System Audio Recording and Microphone access
// for the app that started it (the terminal or the server's host process).
//
// Build: swiftc -O -o recorder/meeting-recorder recorder/MeetingRecorder.swift

import AVFoundation
import Foundation
import ScreenCaptureKit

final class Track {
    let writer: AVAssetWriter
    let input: AVAssetWriterInput
    var started = false

    init(url: URL) throws {
        try? FileManager.default.removeItem(at: url)
        writer = try AVAssetWriter(outputURL: url, fileType: .m4a)
        input = AVAssetWriterInput(mediaType: .audio, outputSettings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 64_000,
        ])
        input.expectsMediaDataInRealTime = true
        writer.add(input)
    }

    func append(_ buffer: CMSampleBuffer) {
        guard buffer.isValid, CMSampleBufferGetNumSamples(buffer) > 0 else { return }
        if !started {
            guard writer.startWriting() else { return }
            writer.startSession(atSourceTime: buffer.presentationTimeStamp)
            started = true
        }
        if input.isReadyForMoreMediaData { input.append(buffer) }
    }

    func finish() async {
        guard started else { return }
        input.markAsFinished()
        await writer.finishWriting()
    }
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let app: Track
    let mic: Track
    var stream: SCStream?
    let queue = DispatchQueue(label: "meeting-recorder.audio")

    init(dir: URL) throws {
        app = try Track(url: dir.appendingPathComponent("app.m4a"))
        mic = try Track(url: dir.appendingPathComponent("mic.m4a"))
    }

    func start(bundleID: String) async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw Failure("no display") }
        let apps = content.applications.filter { $0.bundleIdentifier == bundleID }
        guard !apps.isEmpty else { throw Failure("\(bundleID) is not running") }

        let filter = SCContentFilter(display: display, including: apps, exceptingWindows: [])
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.captureMicrophone = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48_000
        config.channelCount = 1
        // Video cannot be turned off; keep it as small and slow as allowed.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try stream.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue)
        try await stream.startCapture()
        self.stream = stream
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        switch type {
        case .audio: app.append(buffer)
        case .microphone: mic.append(buffer)
        default: break
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write("stream stopped: \(error.localizedDescription)\n".data(using: .utf8)!)
        Task { await self.stop(); exit(2) }
    }

    func stop() async {
        try? await stream?.stopCapture()
        stream = nil
        await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
            queue.async { done.resume() }  // drain buffers already queued
        }
        await app.finish()
        await mic.finish()
    }
}

struct Failure: Error, CustomStringConvertible {
    let description: String
    init(_ d: String) { description = d }
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: meeting-recorder <out-dir> [bundle-id]\n".data(using: .utf8)!)
    exit(64)
}
let dir = URL(fileURLWithPath: args[1], isDirectory: true)
let bundleID = args.count >= 3 ? args[2] : "com.google.Chrome"
try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

let recorder: Recorder
do {
    recorder = try Recorder(dir: dir)
} catch {
    FileHandle.standardError.write("cannot open output: \(error)\n".data(using: .utf8)!)
    exit(1)
}

var signalSources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler {
        Task {
            await recorder.stop()
            print("stopped")
            exit(0)
        }
    }
    source.resume()
    signalSources.append(source)
}

Task {
    do {
        try await recorder.start(bundleID: bundleID)
        print("recording")
        fflush(stdout)
    } catch {
        FileHandle.standardError.write("cannot start: \(error)\n".data(using: .utf8)!)
        exit(1)
    }
}
dispatchMain()
