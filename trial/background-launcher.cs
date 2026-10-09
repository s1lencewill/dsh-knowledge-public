using System;
using System.Diagnostics;
using System.IO;
using System.Text;
// No shell, elevation, service credentials, or arbitrary script string.
// argv: Node executable, installed service-task.mjs, supervise|revive, target.
internal static class BackgroundLauncher {
  private static string Quote(string value) {
    var b=new StringBuilder("\"");int slashes=0;
    foreach(char c in value){if(c=='\\'){slashes++;continue;}if(c=='"'){b.Append('\\',slashes*2+1);b.Append(c);}else {b.Append('\\',slashes);b.Append(c);}slashes=0;}
    b.Append('\\',slashes*2);b.Append('"');return b.ToString();
  }
  [STAThread] private static int Main(string[] args) {
    try {
      if(args.Length!=4 || !Path.IsPathRooted(args[0]) || !Path.IsPathRooted(args[1]) || !Path.IsPathRooted(args[3]) || (args[2]!="supervise" && args[2]!="revive")) return 64;
      string script=Path.GetFullPath(Path.Combine(args[3],"app","trial","service-task.mjs"));
      if(!String.Equals(script,Path.GetFullPath(args[1]),StringComparison.OrdinalIgnoreCase)||!File.Exists(script)||!File.Exists(args[0]))return 65;
      var info=new ProcessStartInfo(args[0],Quote(script)+" "+args[2]+" --target "+Quote(Path.GetFullPath(args[3])));
      info.UseShellExecute=false;info.CreateNoWindow=true;info.WorkingDirectory=Path.GetDirectoryName(script);
      using(var child=Process.Start(info)){child.WaitForExit();return child.ExitCode;}
    }catch {return 70;}
  }
}
