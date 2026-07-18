namespace MiniMaxAIDocx.Core.OpenXml;

/// <summary>
/// Resolve a CLI flag value that is allowed to be EITHER a file path OR an inline
/// JSON literal. LLM/sandbox callers that cannot stage a file on disk pass the
/// object literally as the flag value; classic CLI callers pass a path.
/// </summary>
public static class JsonArg
{
    /// <summary>
    /// Try to obtain the JSON text for <paramref name="value"/>.
    /// Returns the JSON string on success, or null (after writing a warning to
    /// stderr) when the value is neither an inline JSON literal nor a readable file.
    /// </summary>
    /// <param name="value">The raw flag value — inline JSON or a path.</param>
    /// <param name="flagName">The flag name (e.g. "content-json") for warning output.</param>
    /// <param name="allowArray">Whether a top-level array (first non-ws char '[') is acceptable.</param>
    public static string? Resolve(string value, string flagName, bool allowArray)
    {
        // Check for an inline JSON literal FIRST — cheap, no syscall, and also
        // avoids the various IOException variants that .NET throws when an
        // arbitrary JSON blob is mis-parsed as a file path (especially on Linux
        // where almost anything is a legal filename).
        var trimmed = value.TrimStart();
        if (trimmed.StartsWith("{") || (allowArray && trimmed.StartsWith("[")))
        {
            return value;
        }

        try
        {
            return System.IO.File.ReadAllText(value);
        }
        catch (System.IO.IOException) { /* fall through to the warning below */ }

        System.Console.Error.WriteLine(
            $"[{flagName}] value is neither inline JSON nor a readable file: '{value}'");
        return null;
    }
}
