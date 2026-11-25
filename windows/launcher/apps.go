package main

import "strings"

// Fixed product identities from the Microsoft Store catalog, ordered by current channel.
type channel struct {
	family, appID, name string
	checkChatGPTLabel   bool
}

func (c channel) aumid() string { return c.family + "!" + c.appID }

var appChannels = map[string][]channel{
	"ChatGPT": {
		{"OpenAI.Codex_2p2nqsd0c76g0", "App", "ChatGPT", true},
		{"OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0", "ChatGPT", "ChatGPT Classic", false},
	},
	"Claude": {{"AnthropicPBC.Claude_fnn82j28hfe8t", "Claude", "Claude (Microsoft Store)", false}},
}

type registeredApp struct {
	installed, store, development, registered bool
	fullName, label                           string
}
type appStatus struct {
	App             string `json:"app"`
	Status          string `json:"status"`
	Channel         string `json:"channel"`
	AUMID           string `json:"aumid"`
	PackageFullName string `json:"packageFullName"`
	ProcessID       uint32 `json:"processId,omitempty"`
}

// Return activation evidence only after Windows accepts the exact verified AUMID.
func officialApp(opt options, inspect func(channel) (registeredApp, *failure), activate func(string) (uint32, *failure)) (appStatus, *failure) {
	var empty appStatus
	channels := appChannels[opt.app]
	if len(channels) == 0 {
		return empty, fail("arguments", "未识别的官方应用。")
	}
	var installed bool
	for _, c := range channels {
		reg, err := inspect(c)
		if err != nil {
			return empty, err
		}
		installed = installed || reg.installed
		if !reg.installed || !reg.store || reg.development || !reg.registered || reg.fullName == "" {
			continue
		}
		// Before the product rename, this same Store family and AUMID opened Codex.
		if c.checkChatGPTLabel {
			words := strings.Fields(reg.label)
			if len(words) == 0 || !strings.EqualFold(words[0], "ChatGPT") {
				continue
			}
		}
		status := appStatus{App: opt.app, Status: "verified", Channel: c.name, AUMID: c.aumid(), PackageFullName: reg.fullName}
		if opt.check {
			return status, nil
		}
		pid, problem := activate(status.AUMID)
		if problem != nil {
			return empty, problem
		}
		if pid == 0 {
			return empty, fail("activation-pid", "Windows 激活请求未返回应用进程标识。请在开始菜单中检查应用。")
		}
		status.ProcessID = pid
		status.Status = "activation-requested"
		return status, nil
	}
	if installed {
		return empty, fail("app-unverified", "已找到相关包，但其 Microsoft Store 来源、应用注册或产品名称未通过验证。请在 Microsoft Store 中检查 "+opt.app+" 的更新。")
	}
	message := "当前 Windows 用户未安装可验证的 Microsoft Store 版 " + opt.app + "。请在 Microsoft Store 安装官方版，或从开始菜单使用已有的厂商直装版。"
	if opt.app == "Claude" {
		message += "Ubuntu 中也可使用 Anthropic 官方 Linux 客户端。"
	}
	return empty, fail("app-not-installed", message)
}
