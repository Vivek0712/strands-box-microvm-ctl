"""Drive the playground in a real browser against the live service, assert on what it shows, and save
screenshots and a video for the write-up.

    python playground/server.py &                  # or a deployed URL
    python playground/e2e.py --url http://127.0.0.1:8770 --out media

Launches two strands-box microVMs through the Fleet tab, runs a task, fans out, leases, and drains
everything it launched at the end.
"""

import argparse
import os
import time

from playwright.sync_api import expect, sync_playwright


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8770")
    ap.add_argument("--out", default="media")
    ap.add_argument("--key", default=os.environ.get("PLAYGROUND_KEY", ""))
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--reuse", action="store_true",
                    help="use the RUNNING VMs already there: no launch, no lease run, no drain (for a deployed playground "
                         "whose hourly launch budget is spent)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    shot = lambda page, name: page.screenshot(path=os.path.join(a.out, f"{name}.png"), full_page=False)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not a.headed)
        if a.key:  # what an anonymous visitor sees: the lock banner and the public Benchmarks tab
            anon = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
            pa = anon.new_page()
            pa.goto(a.url)
            expect(pa.locator("#locked")).to_be_visible(timeout=30000)
            expect(pa.locator("#bench-body svg").first).to_be_visible(timeout=30000)
            pa.screenshot(path=os.path.join(a.out, "00-anonymous.png"))
            anon.close()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2,
                                  color_scheme="light", record_video_dir=os.path.join(a.out, "video"),
                                  record_video_size={"width": 1440, "height": 900})
        if a.key:
            ctx.add_init_script(f"localStorage.setItem('sbx.key', JSON.stringify({a.key!r}))")
        ctx.add_init_script("localStorage.removeItem('sbx.steps'); localStorage.removeItem('sbx.tab')")
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(a.url)
        expect(page.locator("#chip-region b")).not_to_have_text("-", timeout=30000)
        shot(page, "01-run-empty")

        # Fleet: launch two microVMs and wait for RUNNING.
        page.click("nav.tabs button[data-view=fleet]")
        want = 1 if a.reuse else 2
        if not a.reuse:
            page.fill("#launch-n", "2")
            page.click("#launch")
            expect(page.locator(".toast.good").first).to_contain_text("Launched", timeout=60000)
        deadline = time.time() + 120
        while time.time() < deadline:
            if page.locator("#fleet-table tbody tr:has(.dot.RUNNING)").count() >= want:
                break
            page.wait_for_timeout(1500)
        assert page.locator("#fleet-table tbody tr:has(.dot.RUNNING)").count() >= want, "no RUNNING VMs"
        shot(page, "02-fleet-running")
        page.locator("#fleet-table tbody tr:has(.dot.RUNNING) button:has-text('Inside')").first.click()
        expect(page.locator("#info-body .stat").first).to_be_visible(timeout=60000)
        expect(page.locator("#info-body")).to_contain_text("passed")
        shot(page, "03-fleet-inside")

        # Run: the default steps must permit the reads and deny .env and the delete.
        page.click("nav.tabs button[data-view=run]")
        page.click("#run")
        expect(page.locator("#result .stats")).to_be_visible(timeout=90000)
        expect(page.locator("#result")).to_contain_text("no_env")
        expect(page.locator("#result")).to_contain_text("no_deletes")
        expect(page.locator("#result .step-card pre.err").first).to_contain_text("policy denied")
        shot(page, "04-run-result")
        page.locator("#result-card").screenshot(path=os.path.join(a.out, "04b-run-result-card.png"))
        page.check("#only-deny")
        page.locator("#result .table-wrap").scroll_into_view_if_needed()
        shot(page, "05-run-denials")

        # Custom step: writing outside out/ is refused, inside out/ is permitted.
        page.click("#clear-steps")
        page.click(".preset:has-text('Write out/report.txt')")
        page.click(".preset:has-text('Write outside out/')")
        page.click(".preset:has-text('Read /etc/passwd')")
        page.fill("#custom", "cat out/report.txt")
        page.click("#add-custom")
        page.click("#run")
        expect(page.locator("#result")).to_contain_text("built in a box", timeout=90000)
        shot(page, "06-run-writes")

        # Dark mode, same page.
        page.click("#theme")
        page.wait_for_timeout(300)
        shot(page, "07-run-dark")
        page.click("#theme")

        # Fan-out with Fleet.dispatch.
        page.click("#clear-steps")
        for label in ("List the project", "Read README.md", "Read .env", "Delete scratch.txt"):
            page.click(f".preset:has-text('{label}')")
        page.click("nav.tabs button[data-view=fanout]")
        page.fill("#fo-n", "24" if a.reuse else "48")
        page.fill("#fo-k", "4")
        page.click("#fo-run")
        expect(page.locator("#fo-body .stats")).to_be_visible(timeout=180000)
        expect(page.locator("#fo-body")).to_contain_text("no_env")
        shot(page, "08-fanout")

        # Leases: plan sentence, launch two, follow to done.
        page.click("nav.tabs button[data-view=leases]")
        page.fill("#ls-n", "12")
        page.dispatch_event("#ls-n", "input")
        expect(page.locator("#ls-plan")).to_contain_text("waves", timeout=15000)
        shot(page, "09-lease-plan-waves")
        if not a.reuse:
            page.fill("#ls-n", "2")
            page.dispatch_event("#ls-n", "input")
            expect(page.locator("#ls-plan")).to_contain_text("2 shards", timeout=15000)
            page.fill("#ls-b", "6")
            page.click("#ls-run")
            expect(page.locator("#ls-sum")).to_contain_text("2/2 done", timeout=180000)
            shot(page, "10-lease-done")
            page.click("#ls-stop")

        # Policy, benchmarks, activity.
        page.click("nav.tabs button[data-view=policy]")
        expect(page.locator("#policy-code")).to_contain_text("no_deletes")
        shot(page, "11-policy")
        page.click("nav.tabs button[data-view=bench]")
        expect(page.locator("#bench-body svg").first).to_be_visible(timeout=15000)
        page.wait_for_timeout(500)
        shot(page, "12-bench")
        page.mouse.wheel(0, 700)
        page.wait_for_timeout(300)
        box = page.locator("#bench-body .chart svg").first.bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.4)
        page.wait_for_timeout(300)
        shot(page, "13-bench-hover")
        page.click("nav.tabs button[data-view=activity]")
        expect(page.locator("#trace-table tbody tr").first).to_be_visible()
        shot(page, "14-activity")

        # Phone width.
        page.set_viewport_size({"width": 390, "height": 844})
        page.click("nav.tabs button[data-view=run]")
        page.wait_for_timeout(300)
        overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        shot(page, "15-mobile")
        page.set_viewport_size({"width": 1440, "height": 900})

        # Clean up: drain everything the run launched.
        page.click("nav.tabs button[data-view=fleet]")
        if a.reuse:
            ctx.close()
            browser.close()
            print("page errors:", errors or "none")
            print("horizontal overflow at 390 px:", overflow)
            return 1 if errors or overflow else 0
        page.click("button[data-fleet=drain]")
        page.click("#confirm-yes")
        expect(page.locator(".toast.good").last).to_contain_text("drain", timeout=60000)
        shot(page, "16-drained")
        ctx.close()
        browser.close()
    print("page errors:", errors or "none")
    print("horizontal overflow at 390 px:", overflow)
    return 1 if errors or overflow else 0


if __name__ == "__main__":
    raise SystemExit(main())
