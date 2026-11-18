using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

[assembly: System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8", FrameworkDisplayName = ".NET Framework 4.8")]

internal sealed class DesktopOptions
{
    internal string Root, Home, Project, Python;

    internal static DesktopOptions Parse(string[] args)
    {
        var options = new Dictionary<string, string>(StringComparer.Ordinal);
        var supported = new[] { "--root", "--home", "--project", "--python" };
        for (int i = 0; i < args.Length; i += 2)
        {
            if (!supported.Contains(args[i]) || i + 1 >= args.Length || string.IsNullOrWhiteSpace(args[i + 1]))
                throw new ArgumentException("启动参数需要成对填写：--root、--home、--project、--python。");
            options[args[i]] = args[i + 1];
        }

        string root;
        if (options.TryGetValue("--root", out root))
            root = Path.GetFullPath(root);
        else
            root = FindSource(AppDomain.CurrentDomain.BaseDirectory) ?? FindSource(Environment.CurrentDirectory);
        if (root == null || !File.Exists(Path.Combine(root, "ccaw", "desktop_rpc.py")))
            throw new DirectoryNotFoundException("未找到 CCAW 源码。请从源码目录运行 Start-CCAW.cmd。");

        string home, project, python;
        if (!options.TryGetValue("--home", out home))
            home = Environment.GetEnvironmentVariable("CCAW_HOME");
        if (!options.TryGetValue("--project", out project))
            project = root;
        if (!options.TryGetValue("--python", out python))
        {
            python = Path.Combine(root, ".venv", "Scripts", "python.exe");
            if (!File.Exists(python))
                python = "python.exe";
        }
        return new DesktopOptions
        {
            Root = root,
            Home = Path.GetFullPath(string.IsNullOrWhiteSpace(home) ? Path.Combine(root, ".ccaw") : home),
            Project = Path.GetFullPath(project),
            Python = python
        };
    }

    private static string FindSource(string path)
    {
        for (var directory = new DirectoryInfo(path); directory != null; directory = directory.Parent)
            if (File.Exists(Path.Combine(directory.FullName, "ccaw", "desktop_rpc.py")))
                return directory.FullName;
        return null;
    }
}

internal sealed class LedgerConnection : IDisposable
{
    private readonly DesktopOptions options;
    private readonly SemaphoreSlim gate = new SemaphoreSlim(1, 1);
    private Process process;
    private StreamWriter input;

    internal LedgerConnection(DesktopOptions options)
    {
        this.options = options;
    }

    internal async Task<string> RequestAsync(string message)
    {
        await gate.WaitAsync();
        try
        {
            if (process == null || process.HasExited)
                Start();
            await input.WriteLineAsync(message);
            await input.FlushAsync();
            var response = await process.StandardOutput.ReadLineAsync();
            if (response == null)
                throw new IOException("本地账本服务已退出。请先刷新账本核对操作结果。");
            return response;
        }
        finally
        {
            gate.Release();
        }
    }

    private void Start()
    {
        DisposeProcess();
        var args = new[] { "-m", "ccaw.desktop_rpc", "--home", options.Home, "--project", options.Project };
        var settings = new ProcessStartInfo(options.Python, string.Join(" ", args.Select(QuoteArgument)))
        {
            WorkingDirectory = options.Root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };
        settings.EnvironmentVariables["PYTHONUTF8"] = "1";
        settings.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        try
        {
            process = Process.Start(settings);
            input = new StreamWriter(process.StandardInput.BaseStream, new UTF8Encoding(false));
            process.BeginErrorReadLine();
        }
        catch (Win32Exception)
        {
            throw new IOException("找不到可用的 Python。请安装 Python 3.11 或更新版本，或用 --python 指定 python.exe。");
        }
    }

    private static string QuoteArgument(string value)
    {
        var text = new StringBuilder("\"");
        int backslashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
            {
                backslashes++;
                continue;
            }
            text.Append('\\', character == '"' ? backslashes * 2 + 1 : backslashes);
            text.Append(character);
            backslashes = 0;
        }
        text.Append('\\', backslashes * 2).Append('"');
        return text.ToString();
    }

    private void DisposeProcess()
    {
        try { input?.Dispose(); }
        catch (IOException) { }
        input = null;
        process?.Dispose();
        process = null;
    }

    public void Dispose()
    {
        DisposeProcess();
        gate.Dispose();
    }
}

internal sealed class DesktopWindow : Window
{
    private const string Host = "ccaw.local.example";
    private const string Page = "https://" + Host + "/index.html";
    private const int RequestLimit = 256 * 1024;
    private static readonly HashSet<string> AllowedResources = new HashSet<string>(StringComparer.Ordinal)
    {
        Page, "https://" + Host + "/app.css", "https://" + Host + "/app.js"
    };

    private readonly DesktopOptions options;
    private readonly WebView2 view = new WebView2();
    private readonly LedgerConnection ledger;
    private readonly JavaScriptSerializer json = new JavaScriptSerializer
    {
        MaxJsonLength = int.MaxValue,
        RecursionLimit = 48
    };
    private int pending;
    private bool closeRequested;

    internal DesktopWindow(DesktopOptions options)
    {
        this.options = options;
        ledger = new LedgerConnection(options);
        Title = "CCAW · 本地工作台";
        Width = Math.Min(1160, SystemParameters.WorkArea.Width - 28);
        Height = Math.Min(770, SystemParameters.WorkArea.Height - 28);
        MinWidth = 460;
        MinHeight = 350;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        Content = view;
        Loaded += Initialize;
        Closing += HandleClosing;
        Closed += (sender, args) =>
        {
            view.Dispose();
            ledger.Dispose();
        };
    }

    private async void Initialize(object sender, RoutedEventArgs args)
    {
        Loaded -= Initialize;
        try
        {
            var assets = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "assets");
            if (!File.Exists(Path.Combine(assets, "index.html")))
                throw new FileNotFoundException("找不到桌面页面。请重新构建桌面版，或完整解压发布包。");
            var environment = await CoreWebView2Environment.CreateAsync(
                userDataFolder: Path.Combine(options.Home, "desktop-webview"));
            if (!IsLoaded)
                return;
            await view.EnsureCoreWebView2Async(environment);
            if (!IsLoaded)
                return;
            var core = view.CoreWebView2;
            core.Settings.AreHostObjectsAllowed = false;
            core.Settings.IsWebMessageEnabled = true;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.AreDefaultScriptDialogsEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.NavigationStarting += (host, eventArgs) => eventArgs.Cancel = eventArgs.Uri != Page;
            core.FrameNavigationStarting += (host, eventArgs) => eventArgs.Cancel = true;
            core.NewWindowRequested += (host, eventArgs) => eventArgs.Handled = true;
            core.DownloadStarting += (host, eventArgs) => eventArgs.Cancel = true;
            core.PermissionRequested += (host, eventArgs) => eventArgs.State = CoreWebView2PermissionState.Deny;
            core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All);
            core.WebResourceRequested += (host, eventArgs) =>
            {
                if (!AllowedResources.Contains(eventArgs.Request.Uri))
                    eventArgs.Response = environment.CreateWebResourceResponse(null, 403, "Forbidden", "");
            };
            core.SetVirtualHostNameToFolderMapping(Host, assets, CoreWebView2HostResourceAccessKind.Deny);
            core.WebMessageReceived += ReceiveMessage;
            core.Navigate(Page);
        }
        catch (Exception error)
        {
            if (IsLoaded)
                Content = new TextBlock
                {
                    Margin = new Thickness(25), TextWrapping = TextWrapping.Wrap,
                    Text = "工作台启动失败。请检查本机 WebView2 Runtime 和账本目录。\n\n" + error.Message
                };
        }
    }

    private void HandleClosing(object sender, CancelEventArgs args)
    {
        if (pending == 0)
            return;
        closeRequested = true;
        args.Cancel = true;
        Title = "CCAW · 正在保存，完成后关闭";
    }

    private async void ReceiveMessage(object sender, CoreWebView2WebMessageReceivedEventArgs args)
    {
        if (args.Source != Page || closeRequested)
            return;
        object id = null;
        pending++;
        try
        {
            var message = args.WebMessageAsJson;
            if (message.Length > RequestLimit)
                throw new ArgumentException("输入内容太长。");
            var request = json.Deserialize<Dictionary<string, object>>(message);
            object op;
            if (request == null || !request.TryGetValue("id", out id) || !(id is int)
                || !request.TryGetValue("op", out op) || !(op is string))
                throw new ArgumentException("工作台请求无效。");
            if ((string)op == "host_info")
            {
                Reply(json.Serialize(new { id, ok = true, result = new { python = options.Python } }));
                return;
            }
            var response = await ledger.RequestAsync(message);
            if ((string)op == "folder_path")
                OpenReturnedFolder(response);
            Reply(response);
        }
        catch (Exception error)
        {
            Reply(json.Serialize(new { id, ok = false, error = error.Message }));
        }
        finally
        {
            pending--;
            if (closeRequested && pending == 0)
                Close();
        }
    }

    private void OpenReturnedFolder(string response)
    {
        var packet = json.Deserialize<Dictionary<string, object>>(response);
        object valid;
        if (!packet.TryGetValue("ok", out valid) || !Equals(valid, true))
            return;
        var path = (string)((Dictionary<string, object>)packet["result"])["path"];
        if (!Directory.Exists(path))
            throw new DirectoryNotFoundException("作业目录暂未生成。");
        using (Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }))
        {
        }
    }

    private void Reply(string response)
    {
        if (!closeRequested && view.CoreWebView2 != null)
            view.CoreWebView2.PostWebMessageAsJson(response);
    }
}

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            var options = DesktopOptions.Parse(args);
            return new Application().Run(new DesktopWindow(options));
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "CCAW 启动失败", MessageBoxButton.OK, MessageBoxImage.Error);
            return 2;
        }
    }
}
