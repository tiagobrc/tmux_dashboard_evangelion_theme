// Screensaver that shows the tmux dashboard mirror served by ~/.local/bin/dash-web.
// Build + install with ./build.sh. The URL is fixed to localhost: nothing leaves the machine.
import ScreenSaver
import WebKit

@objc(DashSaverView)
final class DashSaverView: ScreenSaverView, WKNavigationDelegate {
    private static let page = URL(string: "http://127.0.0.1:8765/")!
    private var web: WKWebView?

    override init?(frame: NSRect, isPreview: Bool) {
        super.init(frame: frame, isPreview: isPreview)
        setUp()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        setUp()
    }

    private func setUp() {
        wantsLayer = true
        layer?.backgroundColor = NSColor.black.cgColor
        let view = WKWebView(frame: bounds, configuration: WKWebViewConfiguration())
        view.autoresizingMask = [.width, .height]
        view.navigationDelegate = self
        view.setValue(false, forKey: "drawsBackground")   // no white flash before the page paints
        addSubview(view)
        web = view
        // macOS 14+ keeps old saver instances alive after the saver stops; blank the page so it stops polling.
        DistributedNotificationCenter.default().addObserver(
            self, selector: #selector(saverWillStop), name: Notification.Name("com.apple.screensaver.willstop"), object: nil)
        reload()
    }

    private func reload() {
        web?.load(URLRequest(url: Self.page, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10))
    }

    @objc private func saverWillStop() {
        if !isPreview { web?.loadHTMLString("", baseURL: nil) }
    }

    override func startAnimation() {
        super.startAnimation()
        if web?.url != Self.page { reload() }
    }

    // dash-web not up yet (e.g. right after login): try again shortly
    private func retrySoon() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in self?.reload() }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { retrySoon() }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { retrySoon() }

    override var hasConfigureSheet: Bool { false }
    override var configureSheet: NSWindow? { nil }
}
