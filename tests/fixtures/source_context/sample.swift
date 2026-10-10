import SwiftUI
protocol Runner {
  func run()
}
actor Worker {}
struct Card: View {
  @State var count = 0
  var body: some View {
    Text("Hi")
  }
  init() {}
  func run() {}
}
extension Card {
  func extra() {}
}
