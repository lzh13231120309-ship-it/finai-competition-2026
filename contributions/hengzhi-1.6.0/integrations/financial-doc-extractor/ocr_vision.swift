import Foundation
import Vision
import AppKit
// Input is an image rendered locally; no network or document instructions are executed.
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = false
do {
    try VNImageRequestHandler(url: url).perform([request])
} catch {
    FileHandle.standardError.write(Data("Apple Vision failed: \(error)\n".utf8))
    exit(1)
}
let rows: [[String: Any]] = (request.results ?? []).compactMap { observation in
    guard let candidate = observation.topCandidates(1).first else { return nil }
    let b = observation.boundingBox
    return ["text": candidate.string, "confidence": candidate.confidence,
            "box": [b.minX, 1-b.maxY, b.maxX, 1-b.minY]]
}
let data = try JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
