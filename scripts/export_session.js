// 在已登录的 sports.sjtu.edu.cn 页面的开发者工具 Console 中执行。
// 仅向同一站点请求当前用户；会话复制到本机剪贴板，不上传给其他服务。
(async () => {
  const origin = "https://sports.sjtu.edu.cn";
  if (location.origin !== origin) {
    throw new Error("请在已登录的交大体育预约页面执行此脚本。");
  }
  if (typeof copy !== "function") {
    throw new Error("请在 Chrome/Edge 的开发者工具 Console 中执行。");
  }
  const response = await fetch(`${origin}/system/user/currentUser`, {
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
  });
  if (!response.ok) {
    throw new Error(`当前用户检查失败（HTTP ${response.status}），未复制会话。`);
  }
  const result = await response.json();
  if (
    !(result.code === 0 || result.msg === "操作成功") ||
    !result.data ||
    typeof result.data.loginName !== "string" ||
    !result.data.loginName.trim()
  ) {
    throw new Error("服务器未确认登录，请先在体育预约网站完成登录。");
  }
  const snapshot = (storage) =>
    Object.fromEntries(
      Array.from({ length: storage.length }, (_, index) => {
        const key = storage.key(index);
        return [key, storage.getItem(key)];
      }),
    );
  const exported = {
    schema_version: 1,
    origin,
    exported_at: new Date().toISOString(),
    // 页面脚本无法读取 HttpOnly Cookie；只导出当前可见的 Cookie。
    visible_cookie_header: document.cookie,
    // 登录标识来自本人已认证的当前用户响应；不导出其他个人资料。
    server_session_id: result.data.params?.JSESSIONID || "",
    local_storage: snapshot(localStorage),
    session_storage: snapshot(sessionStorage),
  };
  copy(JSON.stringify(exported, null, 2));
  console.info("登录会话已复制到本机剪贴板。请回到项目终端运行 scripts/import_session.ps1；不要把剪贴板内容发到聊天里。");
})().catch(() => {
  console.error("会话导出未完成。请确认当前页面为已登录的体育预约网站，并检查网络连接；不会输出凭据。");
});
