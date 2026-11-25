package main

import "testing"

func TestOpenAppArguments(t *testing.T) {
	for _, app := range []string{"ChatGPT", "Claude"} {
		opt, err := parse([]string{"--native", "--open-app", app, "--check-native"})
		if err != nil || opt.app != app || !opt.check {
			t.Fatalf("valid command: %+v %v", opt, err)
		}
	}
	for _, args := range [][]string{{"--open-app"}, {"--open-app", "ChatGPT.exe"}, {"--open-app", "--distro"}, {"--open-app", "Claude", "--open-app", "ChatGPT"}, {"--distro", "Ubuntu", "--open-app", "Claude"}} {
		if _, err := parse(args); err == nil {
			t.Error("accepted", args)
		}
	}
}

func TestAppIdentityAndActivation(t *testing.T) {
	ready := registeredApp{installed: true, store: true, registered: true, fullName: "registered-current-user-full-name", label: "ChatGPT"}
	missing := registeredApp{}
	direct, dev, helperOnly, oldCodex, ambiguous := ready, ready, ready, ready, ready
	direct.store = false
	dev.development = true
	helperOnly.registered = false
	oldCodex.label = "Codex"
	ambiguous.label = "ChatGPTAssistant"
	const current = "OpenAI.Codex_2p2nqsd0c76g0!App"
	const classic = "OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0!ChatGPT"
	const claude = "AnthropicPBC.Claude_fnn82j28hfe8t!Claude"
	for _, tc := range []struct {
		name, app     string
		first, second registeredApp
		check         bool
		id, code      string
	}{
		{"current", "ChatGPT", ready, ready, false, current, ""},
		{"registered-check", "Claude", ready, missing, true, claude, ""},
		{"claude", "Claude", ready, missing, false, claude, ""},
		{"current-codex-only", "ChatGPT", oldCodex, ready, false, classic, ""},
		{"unrecognized-label", "ChatGPT", ambiguous, missing, false, "", "app-unverified"},
		{"missing-primary", "ChatGPT", missing, ready, false, classic, ""},
		{"direct-unverified", "Claude", direct, missing, false, "", "app-unverified"},
		{"unpacked-dev", "ChatGPT", dev, missing, false, "", "app-unverified"},
		{"helper-only", "ChatGPT", helperOnly, missing, false, "", "app-unverified"},
		{"missing", "Claude", missing, missing, false, "", "app-not-installed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls, activated := 0, 0
			status, err := officialApp(options{app: tc.app, check: tc.check}, func(c channel) (registeredApp, *failure) {
				calls++
				if calls == 1 {
					return tc.first, nil
				}
				if calls != 2 {
					t.Fatal("unexpected discovery")
				}
				return tc.second, nil
			}, func(id string) (uint32, *failure) {
				activated++
				if id != tc.id || tc.id == "" || tc.check {
					t.Errorf("unexpected activation %q", id)
				}
				return 324, nil
			}, nil)
			if tc.code != "" {
				if err == nil || err.code != tc.code || activated != 0 || status != (appStatus{}) {
					t.Fatal(status, err, activated)
				}
				return
			}
			if err != nil || status.AUMID != tc.id || status.PackageFullName != ready.fullName || status.App != tc.app {
				t.Fatal(status, err)
			}
			if tc.check {
				if activated != 0 || status.Status != "verified" || status.ProcessID != 0 {
					t.Fatal(status, activated)
				}
			} else if activated != 1 || status.Status != "activation-requested" || status.ProcessID != 324 {
				t.Fatal(status, activated)
			}
		})
	}
}

func TestWindowsAppErrorsDoNotImplyActivation(t *testing.T) {
	for _, tc := range []struct {
		lookupErr, activateErr *failure
		code                   string
	}{
		{fail("windows-app-api", "lookup error"), nil, "windows-app-api"},
		{nil, fail("windows-app-api", "no activation"), "windows-app-api"},
		{nil, nil, "activation-pid"},
	} {
		n := 0
		s, e := officialApp(options{app: "Claude"}, func(c channel) (registeredApp, *failure) {
			return registeredApp{installed: true, store: true, registered: true, fullName: "family-full-name"}, tc.lookupErr
		}, func(_ string) (uint32, *failure) { n++; return 0, tc.activateErr }, nil)
		if s != (appStatus{}) || e == nil || e.code != tc.code || (tc.lookupErr != nil && n != 0) {
			t.Fatal(s, e, n)
		}
	}
}

func TestInstallOfferRequiresAnInteractiveMissingApp(t *testing.T) {
	ready := registeredApp{installed: true, store: true, registered: true, fullName: "current"}
	for _, tc := range []struct {
		name                  string
		args                  []string
		reg                   registeredApp
		lookupFailure         bool
		wantStatus, wantError string
		offers                int
	}{
		{"interactive missing", []string{"--open-app", "Claude", "--offer-install"}, registeredApp{}, false, "cancelled", "", 1},
		{"programmatic missing", []string{"--open-app", "Claude"}, registeredApp{}, false, "", "app-not-installed", 0},
		{"check missing", []string{"--open-app", "Claude", "--check-native", "--offer-install"}, registeredApp{}, false, "", "app-not-installed", 0},
		{"installed", []string{"--open-app", "Claude", "--offer-install"}, ready, false, "activation-requested", "", 0},
		{"check installed", []string{"--open-app", "Claude", "--offer-install", "--check-native"}, ready, false, "verified", "", 0},
		{"unverified", []string{"--open-app", "Claude", "--offer-install"}, registeredApp{installed: true}, false, "", "app-unverified", 0},
		{"lookup failed", []string{"--open-app", "Claude", "--offer-install"}, registeredApp{}, true, "", "windows-app-api", 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			opt, argErr := parse(tc.args)
			if argErr != nil {
				t.Fatal(argErr)
			}
			offers, activations := 0, 0
			status, err := officialApp(opt, func(c channel) (registeredApp, *failure) {
				if tc.lookupFailure {
					return registeredApp{}, fail("windows-app-api", "lookup")
				}
				return tc.reg, nil
			}, func(string) (uint32, *failure) { activations++; return 55, nil }, func(app string) (appStatus, *failure) {
				if app != "Claude" {
					t.Errorf("unexpected app %q", app)
				}
				offers++
				return appStatus{App: app, Status: "cancelled"}, nil
			})
			if (tc.wantError == "") != (err == nil) || (err != nil && err.code != tc.wantError) || status.Status != tc.wantStatus || offers != tc.offers ||
				(activations == 1) != (tc.wantStatus == "activation-requested") {
				t.Fatalf("status=%#v err=%v offers=%d activations=%d", status, err, offers, activations)
			}
		})
	}
	if _, err := parse([]string{"--offer-install"}); err == nil {
		t.Fatal("install offer requires an official app")
	}
}

func TestOfficialInstallPageRequests(t *testing.T) {
	for _, p := range []struct{ app, store, web string }{
		{"ChatGPT", "ms-windows-store://pdp/?ProductId=9PLM9XGG6VKS", "https://chatgpt.com/download/"},
		{"Claude", "ms-windows-store://pdp/?ProductId=9P6K58THS811", "https://claude.com/download"},
	} {
		for _, tc := range []struct {
			name                             string
			accept, dialogOK, storeOK, webOK bool
			wantStatus, wantError            string
			calls                            int
		}{
			{"cancel", false, true, false, false, "cancelled", "", 0},
			{"dialog failure", false, false, false, false, "", "install-dialog-failed", 0},
			{"open store", true, true, true, false, "install-page-requested", "", 1},
			{"default browser", true, true, false, true, "install-page-requested", "", 2},
			{"unable to open either", true, true, false, false, "", "install-page-failed", 2},
		} {
			t.Run(p.app+"/"+tc.name, func(t *testing.T) {
				var urls []string
				prompts := 0
				s, err := offerInstallPage(p.app, func(app string) (bool, *failure) {
					prompts++
					if app != p.app {
						t.Fatalf("unexpected prompt %q", app)
					}
					if !tc.dialogOK {
						return false, fail("install-dialog-failed", "dialog")
					}
					return tc.accept, nil
				}, func(url string) bool {
					urls = append(urls, url)
					if len(urls) == 1 {
						if url != p.store {
							t.Fatalf("unexpected store URL %q", url)
						}
						return tc.storeOK
					}
					if url != p.web {
						t.Fatalf("unexpected browser URL %q", url)
					}
					return tc.webOK
				})
				if prompts != 1 || len(urls) != tc.calls || s.Status != tc.wantStatus || (err == nil) != (tc.wantError == "") || (err != nil && err.code != tc.wantError) {
					t.Fatalf("%#v %v urls=%v prompts=%d", s, err, urls, prompts)
				}
			})
		}
	}
}
