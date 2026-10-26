import SwiftUI
import NexusKit

struct StructuredValueView: View {
    let value: JSONValue
    var body: some View {
        Group {
            switch value {
            case .object(let fields):
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(fields.keys.sorted(), id: \.self) { key in
                        if let child = fields[key] {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(key.replacingOccurrences(of: "_", with: " ").capitalized).font(.headline).accessibilityAddTraits(.isHeader)
                                StructuredValueView(value: child)
                            }
                        }
                    }
                }
            case .array(let values):
                VStack(alignment: .leading, spacing: 10) { ForEach(values.indices, id: \.self) { index in StructuredValueView(value: values[index]) } }
            case .string(let text): Text(text).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
            case .number(let number): Text(String(number)).textSelection(.enabled)
            case .integer(let number): Text(String(number)).textSelection(.enabled)
            case .bool(let flag): Text(flag ? "Yes" : "No")
            case .null: Text("—").foregroundStyle(.secondary)
            }
        }.font(.body)
    }
}
