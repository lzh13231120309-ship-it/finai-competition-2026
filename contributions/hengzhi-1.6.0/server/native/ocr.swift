// Offline OCR: Apple Vision, JSON stdout, top-left pixel coordinates.
import Foundation
import Vision
import ImageIO
import CoreGraphics

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}
guard CommandLine.arguments.count == 2 else { fail("Usage: finocr IMAGE_PATH") }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fail("Cannot decode image")
}
let width = Double(image.width), height = Double(image.height)
func pixels(_ r: CGRect) -> [Double] {
    [Double(r.minX) * width, (1 - Double(r.maxY)) * height,
     Double(r.maxX) * width, (1 - Double(r.minY)) * height]
}
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
request.usesLanguageCorrection = false // Preserve financial signs and numeric strings.
request.minimumTextHeight = 0.003
let handler = VNImageRequestHandler(cgImage: image, options: [:])
do {
    try handler.perform([request])
    var blocks: [[String: Any]] = []
    // Numeric tokens are kept intact, including signs, parentheses, comma groups and percentages.
    let regex = try NSRegularExpression(pattern: #"[（(]?[+−－-]?(?:[¥￥$€£]?\d[\d,，]*(?:[.．]\d+)?[%％]?)[）)]?|[^\s\d+−－()（）,，.．%％]+"#)
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        var tokens: [[String: Any]] = []
        let value = candidate.string
        for match in regex.matches(in: value, range: NSRange(value.startIndex..., in: value)) {
            guard let range = Range(match.range, in: value),
                  let bounds = try? candidate.boundingBox(for: range) else { continue }
            tokens.append(["text": String(value[range]), "bbox": pixels(bounds.boundingBox),
                           "confidence": Double(candidate.confidence)])
        }
        blocks.append(["text": value, "bbox": pixels(observation.boundingBox),
                       "confidence": Double(candidate.confidence), "tokens": tokens])
    }
    let result: [String: Any] = ["width": image.width, "height": image.height, "blocks": blocks,
                               "engine": "Apple Vision", "languages": request.recognitionLanguages]
    let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
} catch { fail("Vision OCR failed: \(error)") }
