// <name>-approve: asks the person at the Mac for Touch ID, or the Mac password
// as the fallback, before an action that sends, deletes or shares.
//
//   bertie-approve "<short reason>"
//
// The policy is .deviceOwnerAuthentication: Touch ID when the Mac has it, and
// the account password when Touch ID is not set up, fails, or is skipped.
// The reason arrives as the one argument and is shown in the macOS sheet.
//
// Exit codes (the only thing the caller trusts):
//   0  the person was verified
//   1  not verified: wrong finger or password, cancelled, or no answer in time
//   2  bad use (not exactly one non-empty argument)
//   3  this Mac cannot ask (no password set, or the check is unavailable)
//
// Built by install.sh with:  swiftc -O -o bertie-approve approve.swift
import Foundation
import LocalAuthentication

let answerSeconds = 60.0       // LAContext has no limit of its own; the sheet could sit forever
let reasonLimit = 100

func flatten(_ text: String) -> String {
    var out = String.UnicodeScalarView()
    for scalar in text.unicodeScalars {
        switch scalar.properties.generalCategory {
        case .control, .format, .lineSeparator, .paragraphSeparator:
            out.append(" ")
        default:
            out.append(scalar)
        }
    }
    let words = String(out).split(separator: " ", omittingEmptySubsequences: true)
    return String(words.joined(separator: " ").prefix(reasonLimit))
}

let args = CommandLine.arguments
let me = (args.first.map { ($0 as NSString).lastPathComponent }) ?? "approve"
guard args.count == 2 else {
    FileHandle.standardError.write(Data("usage: \(me) <reason>\n".utf8))
    exit(2)
}
let reason = flatten(args[1])
guard !reason.isEmpty else {
    FileHandle.standardError.write(Data("\(me): the reason is empty\n".utf8))
    exit(2)
}

let context = LAContext()
var policyError: NSError?
guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &policyError) else {
    FileHandle.standardError.write(Data("\(me): this Mac cannot ask for Touch ID or a password here\n".utf8))
    exit(3)
}

final class Outcome: @unchecked Sendable {
    private let lock = NSLock()
    private var approved = false
    func set(_ value: Bool) { lock.lock(); approved = value; lock.unlock() }
    func get() -> Bool { lock.lock(); defer { lock.unlock() }; return approved }
}

let outcome = Outcome()
let finished = DispatchSemaphore(value: 0)
context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { success, _ in
    outcome.set(success)
    finished.signal()
}

if finished.wait(timeout: .now() + answerSeconds) == .timedOut {
    context.invalidate()
    FileHandle.standardError.write(Data("\(me): no answer in time\n".utf8))
    exit(1)
}
exit(outcome.get() ? 0 : 1)
