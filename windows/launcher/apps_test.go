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
			})
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
		}, func(_ string) (uint32, *failure) { n++; return 0, tc.activateErr })
		if s != (appStatus{}) || e == nil || e.code != tc.code || (tc.lookupErr != nil && n != 0) {
			t.Fatal(s, e, n)
		}
	}
}
