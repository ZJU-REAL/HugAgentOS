using System.CommandLine;
using DocumentFormat.OpenXml;
using DocumentFormat.OpenXml.Packaging;
using DocumentFormat.OpenXml.Wordprocessing;
using MiniMaxAIDocx.Core.OpenXml;
using MiniMaxAIDocx.Core.Typography;

namespace MiniMaxAIDocx.Core.Commands;

/// <summary>
/// Scenario A: Create a new DOCX document from scratch with proper styles, sections,
/// headers/footers, and typography defaults.
/// </summary>
public static class CreateCommand
{
    public static Command Create()
    {
        var outputOption = new Option<string>("--output") { Description = "Output DOCX file path", Required = true };
        var typeOption = new Option<string>("--type") { Description = "Document type: report, letter, memo, academic" };
        typeOption.DefaultValueFactory = _ => "report";
        var titleOption = new Option<string>("--title") { Description = "Document title" };
        var authorOption = new Option<string>("--author") { Description = "Document author" };
        var pageSizeOption = new Option<string>("--page-size") { Description = "Page size: letter, a4, legal, a3" };
        pageSizeOption.DefaultValueFactory = _ => "letter";
        var marginsOption = new Option<string>("--margins") { Description = "Margin preset: standard, narrow, wide" };
        marginsOption.DefaultValueFactory = _ => "standard";
        var headerTextOption = new Option<string>("--header") { Description = "Header text" };
        var footerTextOption = new Option<string>("--footer") { Description = "Footer text" };
        var pageNumbersOption = new Option<bool>("--page-numbers") { Description = "Add page numbers in footer" };
        var tocOption = new Option<bool>("--toc") { Description = "Insert table of contents placeholder" };
        var contentJsonOption = new Option<string>("--content-json") { Description = "Path to JSON file describing document content" };

        var cmd = new Command("create", "Create a new DOCX document from scratch")
        {
            outputOption, typeOption, titleOption, authorOption, pageSizeOption,
            marginsOption, headerTextOption, footerTextOption, pageNumbersOption,
            tocOption, contentJsonOption
        };

        cmd.SetAction((parseResult) =>
        {
            var output = parseResult.GetValue(outputOption)!;
            var docType = parseResult.GetValue(typeOption) ?? "report";
            var title = parseResult.GetValue(titleOption);
            var author = parseResult.GetValue(authorOption);
            var pageSizeName = parseResult.GetValue(pageSizeOption) ?? "letter";
            var marginsName = parseResult.GetValue(marginsOption) ?? "standard";
            var headerText = parseResult.GetValue(headerTextOption);
            var footerText = parseResult.GetValue(footerTextOption);
            var pageNumbers = parseResult.GetValue(pageNumbersOption);
            var tocPlaceholder = parseResult.GetValue(tocOption);
            var contentJson = parseResult.GetValue(contentJsonOption);

            var fontConfig = GetFontConfig(docType);
            var pageSize = GetPageSizeConfig(pageSizeName);
            var margins = GetMargins(marginsName);

            using var doc = WordprocessingDocument.Create(output, WordprocessingDocumentType.Document);
            var mainPart = doc.AddMainDocumentPart();
            mainPart.Document = new Document(new Body());
            var body = mainPart.Document.Body!;

            // Add styles part with defaults
            AddDefaultStyles(mainPart, fontConfig);

            // Add section properties (page size, margins)
            var sectPr = new SectionProperties();
            sectPr.Append(new DocumentFormat.OpenXml.Wordprocessing.PageSize
            {
                Width = (UInt32Value)(uint)pageSize.WidthDxa,
                Height = (UInt32Value)(uint)pageSize.HeightDxa
            });
            sectPr.Append(new PageMargin
            {
                Top = margins.TopDxa,
                Bottom = margins.BottomDxa,
                Left = (UInt32Value)(uint)margins.LeftDxa,
                Right = (UInt32Value)(uint)margins.RightDxa
            });

            // Add header if requested
            if (!string.IsNullOrEmpty(headerText))
            {
                var headerPart = mainPart.AddNewPart<HeaderPart>();
                headerPart.Header = new Header(
                    new Paragraph(new Run(new Text(headerText))));
                var headerRefId = mainPart.GetIdOfPart(headerPart);
                sectPr.Append(new HeaderReference
                {
                    Type = HeaderFooterValues.Default,
                    Id = headerRefId
                });
            }

            // Add footer if requested
            if (!string.IsNullOrEmpty(footerText) || pageNumbers)
            {
                var footerPart = mainPart.AddNewPart<FooterPart>();
                var footerParagraph = new Paragraph();

                if (!string.IsNullOrEmpty(footerText))
                {
                    footerParagraph.Append(new Run(new Text(footerText)));
                }

                if (pageNumbers)
                {
                    if (!string.IsNullOrEmpty(footerText))
                        footerParagraph.Append(new Run(new Text(" — ") { Space = SpaceProcessingModeValues.Preserve }));

                    footerParagraph.Append(new Run(
                        new FieldChar { FieldCharType = FieldCharValues.Begin }));
                    footerParagraph.Append(new Run(
                        new FieldCode(" PAGE ") { Space = SpaceProcessingModeValues.Preserve }));
                    footerParagraph.Append(new Run(
                        new FieldChar { FieldCharType = FieldCharValues.End }));
                }

                footerPart.Footer = new Footer(footerParagraph);
                var footerRefId = mainPart.GetIdOfPart(footerPart);
                sectPr.Append(new FooterReference
                {
                    Type = HeaderFooterValues.Default,
                    Id = footerRefId
                });
            }

            // Title
            if (!string.IsNullOrEmpty(title))
            {
                var titlePara = new Paragraph(
                    new ParagraphProperties(new ParagraphStyleId { Val = "Title" }),
                    new Run(new Text(title)));
                body.Append(titlePara);
            }

            // Author subtitle
            if (!string.IsNullOrEmpty(author))
            {
                var authorPara = new Paragraph(
                    new ParagraphProperties(new ParagraphStyleId { Val = "Subtitle" }),
                    new Run(new Text(author)));
                body.Append(authorPara);
            }

            // TOC placeholder
            if (tocPlaceholder)
            {
                body.Append(new Paragraph(
                    new ParagraphProperties(new ParagraphStyleId { Val = "TOCHeading" }),
                    new Run(new Text("Table of Contents"))));

                // Insert TOC field
                var tocPara = new Paragraph();
                tocPara.Append(new Run(new FieldChar { FieldCharType = FieldCharValues.Begin }));
                tocPara.Append(new Run(new FieldCode(" TOC \\o \"1-3\" \\h \\z \\u ") { Space = SpaceProcessingModeValues.Preserve }));
                tocPara.Append(new Run(new FieldChar { FieldCharType = FieldCharValues.Separate }));
                tocPara.Append(new Run(new Text("Update this field to generate table of contents.")));
                tocPara.Append(new Run(new FieldChar { FieldCharType = FieldCharValues.End }));
                body.Append(tocPara);

                // Page break after TOC
                body.Append(new Paragraph(new Run(new Break { Type = BreakValues.Page })));
            }

            // --content-json accepts either a file path or an inline JSON literal
            // (array or object). See MiniMaxAIDocx.Core.OpenXml.JsonArg.
            if (!string.IsNullOrEmpty(contentJson))
            {
                var jsonContent = JsonArg.Resolve(contentJson, "content-json", allowArray: true);
                if (jsonContent != null)
                {
                    AddContentFromJson(body, jsonContent);
                }
                else
                {
                    Console.Error.WriteLine("Document will be created without body content.");
                }
            }

            // Ensure body has at least one paragraph
            if (!body.Elements<Paragraph>().Any())
            {
                body.Append(new Paragraph());
            }

            // sectPr must be the last child of body
            body.Append(sectPr);

            mainPart.Document.Save();
            Console.WriteLine($"Created {docType} document: {output}");
        });

        return cmd;
    }

    private static FontConfig GetFontConfig(string docType) => docType.ToLowerInvariant() switch
    {
        "letter" => FontDefaults.Letter,
        "memo" => FontDefaults.Memo,
        "academic" => FontDefaults.Academic,
        _ => FontDefaults.Report,
    };

    private static Typography.PageSize GetPageSizeConfig(string name) => name.ToLowerInvariant() switch
    {
        "a4" => PageSizes.A4,
        "legal" => PageSizes.Legal,
        "a3" => PageSizes.A3,
        _ => PageSizes.Letter,
    };

    private static MarginConfig GetMargins(string name) => name.ToLowerInvariant() switch
    {
        "narrow" => PageSizes.NarrowMargins,
        "wide" => PageSizes.WideMargins,
        _ => PageSizes.StandardMargins,
    };

    private static void AddDefaultStyles(MainDocumentPart mainPart, FontConfig fontConfig)
    {
        var stylesPart = mainPart.AddNewPart<StyleDefinitionsPart>();
        var styles = new Styles();

        // Default run properties
        var defaultRPr = new StyleRunProperties(
            new RunFonts { Ascii = fontConfig.BodyFont, HighAnsi = fontConfig.BodyFont },
            new FontSize { Val = UnitConverter.FontSizeToSz(fontConfig.BodySize) },
            new FontSizeComplexScript { Val = UnitConverter.FontSizeToSz(fontConfig.BodySize) });

        // Normal style
        styles.Append(new Style(
            new StyleName { Val = "Normal" },
            new PrimaryStyle(),
            defaultRPr)
        { Type = StyleValues.Paragraph, StyleId = "Normal", Default = true });

        // Heading styles 1-6
        double[] headingSizes = [fontConfig.Heading1Size, fontConfig.Heading2Size, fontConfig.Heading3Size,
                                 fontConfig.Heading4Size, fontConfig.Heading5Size, fontConfig.Heading6Size];
        for (int i = 0; i < 6; i++)
        {
            var level = i + 1;
            var headingStyle = new Style(
                new StyleName { Val = $"heading {level}" },
                new BasedOn { Val = "Normal" },
                new NextParagraphStyle { Val = "Normal" },
                new PrimaryStyle(),
                new StyleParagraphProperties(
                    new KeepNext(),
                    new KeepLines(),
                    new SpacingBetweenLines { Before = "240", After = "120" },
                    new OutlineLevel { Val = i }),
                new StyleRunProperties(
                    new RunFonts { Ascii = fontConfig.HeadingFont, HighAnsi = fontConfig.HeadingFont },
                    new FontSize { Val = UnitConverter.FontSizeToSz(headingSizes[i]) },
                    new FontSizeComplexScript { Val = UnitConverter.FontSizeToSz(headingSizes[i]) },
                    new Bold()))
            { Type = StyleValues.Paragraph, StyleId = $"Heading{level}" };
            styles.Append(headingStyle);
        }

        // Title style
        styles.Append(new Style(
            new StyleName { Val = "Title" },
            new BasedOn { Val = "Normal" },
            new NextParagraphStyle { Val = "Normal" },
            new PrimaryStyle(),
            new StyleParagraphProperties(
                new Justification { Val = JustificationValues.Center },
                new SpacingBetweenLines { After = "300" }),
            new StyleRunProperties(
                new RunFonts { Ascii = fontConfig.HeadingFont, HighAnsi = fontConfig.HeadingFont },
                new FontSize { Val = UnitConverter.FontSizeToSz(fontConfig.Heading1Size + 6) },
                new FontSizeComplexScript { Val = UnitConverter.FontSizeToSz(fontConfig.Heading1Size + 6) }))
        { Type = StyleValues.Paragraph, StyleId = "Title" });

        // Subtitle style
        styles.Append(new Style(
            new StyleName { Val = "Subtitle" },
            new BasedOn { Val = "Normal" },
            new NextParagraphStyle { Val = "Normal" },
            new StyleParagraphProperties(
                new Justification { Val = JustificationValues.Center },
                new SpacingBetweenLines { After = "200" }),
            new StyleRunProperties(
                new Color { Val = "5A5A5A" },
                new FontSize { Val = UnitConverter.FontSizeToSz(fontConfig.BodySize + 2) }))
        { Type = StyleValues.Paragraph, StyleId = "Subtitle" });

        stylesPart.Styles = styles;
        stylesPart.Styles.Save();
    }

    private static void AddContentFromJson(Body body, string jsonContent)
    {
        // Supports two JSON formats:
        //
        // Format A (flat array):
        //   [{"type":"heading","text":"Intro","level":1}, {"type":"paragraph","text":"..."}, ...]
        //
        // Format B (structured sections — what the AI agent naturally produces):
        //   { "sections": [ { "heading":"Title", "level":1, "paragraphs":["..."],
        //                      "table":{"headers":[...],"rows":[[...]]},
        //                      "items":["..."], "list_style":"bullet" }, ... ] }
        //   OR { "content": [...] } OR { "elements": [...] }
        //
        // Each flat element supports:
        //   heading:   {type:"heading",   text:"...", level:1-6}
        //   paragraph: {type:"paragraph", text:"..."}
        //   table:     {type:"table",     headers:["..."], rows:[["..."]]}
        //   list:      {type:"list",      items:["..."], style:"bullet"|"numbered"}
        //   pagebreak: {type:"pagebreak"}
        try
        {
            using var jsonDoc = System.Text.Json.JsonDocument.Parse(jsonContent);
            var root = jsonDoc.RootElement;

            if (root.ValueKind == System.Text.Json.JsonValueKind.Array)
            {
                // Format A: flat array of elements
                foreach (var el in root.EnumerateArray())
                    AddSingleElement(body, el);
            }
            else if (root.ValueKind == System.Text.Json.JsonValueKind.Object)
            {
                // Format B: object wrapper — try sections/content/elements key
                System.Text.Json.JsonElement arr;
                if (root.TryGetProperty("sections", out arr) ||
                    root.TryGetProperty("content", out arr) ||
                    root.TryGetProperty("elements", out arr))
                {
                    if (arr.ValueKind == System.Text.Json.JsonValueKind.Array)
                    {
                        foreach (var el in arr.EnumerateArray())
                            AddSingleElement(body, el);
                    }
                }
                else
                {
                    // Single section object at root level
                    AddSingleElement(body, root);
                }
            }
        }
        catch (System.Text.Json.JsonException ex)
        {
            Console.Error.WriteLine($"Warning: could not parse content JSON: {ex.Message}");
        }
    }

    /// <summary>
    /// Add one content element to the body. Handles both flat elements
    /// (with "type" key) and section-style elements (with "heading"/"paragraphs" keys).
    /// </summary>
    private static void AddSingleElement(Body body, System.Text.Json.JsonElement element)
    {
        if (element.TryGetProperty("type", out var typeProp))
        {
            var rawType = (typeProp.GetString() ?? "paragraph").ToLowerInvariant();
            var norm = NormalizeBlockType(rawType);

            switch (norm.CanonicalType)
            {
                case "heading":
                {
                    var text = element.TryGetProperty("text", out var t) ? t.GetString() ?? "" : "";
                    var level = norm.Level
                        ?? (element.TryGetProperty("level", out var lvl) ? lvl.GetInt32() : 1);
                    level = Math.Clamp(level, 1, 6);
                    body.Append(new Paragraph(
                        new ParagraphProperties(new ParagraphStyleId { Val = $"Heading{level}" }),
                        new Run(new Text(text))));
                    break;
                }

                case "paragraph":
                {
                    var text = element.TryGetProperty("text", out var t) ? t.GetString() ?? "" : "";
                    body.Append(new Paragraph(new Run(new Text(text))));
                    break;
                }

                case "table":
                    AddTable(body, element);
                    break;

                case "list":
                    AddList(body, element, norm.ListStyle);
                    break;

                case "pagebreak":
                    body.Append(new Paragraph(new Run(new Break { Type = BreakValues.Page })));
                    break;

                default:
                    // Fall back to rendering the block's "text" field as a plain
                    // paragraph so content isn't silently lost on an unknown type.
                    {
                        var text = element.TryGetProperty("text", out var t) ? t.GetString() ?? "" : "";
                        if (!string.IsNullOrEmpty(text))
                        {
                            Console.Error.WriteLine($"[create] unknown block type '{rawType}', rendering as plain paragraph.");
                            body.Append(new Paragraph(new Run(new Text(text))));
                        }
                        else
                        {
                            Console.Error.WriteLine($"[create] unknown block type '{rawType}' with no text; skipping.");
                        }
                    }
                    break;
            }
        }
        else
        {
            // Structured section format: { heading, level, paragraphs, table, items, ... }
            if (element.TryGetProperty("heading", out var headingProp))
            {
                var heading = headingProp.GetString() ?? "";
                var level = element.TryGetProperty("level", out var lvl) ? lvl.GetInt32() : 1;
                level = Math.Clamp(level, 1, 6);
                body.Append(new Paragraph(
                    new ParagraphProperties(new ParagraphStyleId { Val = $"Heading{level}" }),
                    new Run(new Text(heading))));
            }

            // Paragraphs array
            if (element.TryGetProperty("paragraphs", out var paragraphs) &&
                paragraphs.ValueKind == System.Text.Json.JsonValueKind.Array)
            {
                foreach (var p in paragraphs.EnumerateArray())
                {
                    var text = p.ValueKind == System.Text.Json.JsonValueKind.String
                        ? p.GetString() ?? ""
                        : (p.TryGetProperty("text", out var pt) ? pt.GetString() ?? "" : "");
                    body.Append(new Paragraph(new Run(new Text(text))));
                }
            }

            // Single "text" field (alternative to paragraphs)
            if (element.TryGetProperty("text", out var textProp) &&
                textProp.ValueKind == System.Text.Json.JsonValueKind.String)
            {
                body.Append(new Paragraph(new Run(new Text(textProp.GetString() ?? ""))));
            }

            // Inline table
            if (element.TryGetProperty("table", out var tableProp) &&
                tableProp.ValueKind == System.Text.Json.JsonValueKind.Object)
            {
                AddTable(body, tableProp);
            }

            // Inline list / items
            if (element.TryGetProperty("items", out var itemsProp) &&
                itemsProp.ValueKind == System.Text.Json.JsonValueKind.Array)
            {
                var listStyle = element.TryGetProperty("list_style", out var ls)
                    ? ls.GetString() ?? "bullet"
                    : "bullet";
                AddListItems(body, itemsProp, listStyle);
            }

            // Nested sub-sections
            if (element.TryGetProperty("sections", out var subSections) &&
                subSections.ValueKind == System.Text.Json.JsonValueKind.Array)
            {
                foreach (var sub in subSections.EnumerateArray())
                    AddSingleElement(body, sub);
            }
        }
    }

    /// <summary>
    /// Normalize an LLM-supplied block type name to the canonical set
    /// (`heading` / `paragraph` / `table` / `list` / `pagebreak`).
    ///
    /// LLMs often reach for markdown/HTML names they know from other block
    /// systems (minimax-pdf, markdown, HTML). Mapping those to canonical types
    /// means a wrong guess still produces body content instead of being
    /// silently dropped by the switch in AddSingleElement.
    /// </summary>
    private static (string CanonicalType, int? Level, string? ListStyle) NormalizeBlockType(string rawType)
    {
        if (rawType.Length == 2 && rawType[0] == 'h' && char.IsDigit(rawType[1]))
            return ("heading", rawType[1] - '0', null);

        return rawType switch
        {
            "p" or "body" or "text"                      => ("paragraph", null, null),
            "ul" or "bullet" or "bullets"                => ("list", null, "bullet"),
            "ol" or "numbered"                           => ("list", null, "numbered"),
            "hr" or "page-break" or "br"                 => ("pagebreak", null, null),
            _                                            => (rawType, null, null),
        };
    }

    /// <summary>
    /// Add a table from a JSON element containing "headers" and "rows".
    /// </summary>
    private static void AddTable(Body body, System.Text.Json.JsonElement tableEl)
    {
        var table = new Table();

        // Table properties: borders + auto-fit
        var tblPr = new TableProperties(
            new TableBorders(
                new TopBorder { Val = BorderValues.Single, Size = 4, Color = "auto" },
                new BottomBorder { Val = BorderValues.Single, Size = 4, Color = "auto" },
                new LeftBorder { Val = BorderValues.Single, Size = 4, Color = "auto" },
                new RightBorder { Val = BorderValues.Single, Size = 4, Color = "auto" },
                new InsideHorizontalBorder { Val = BorderValues.Single, Size = 4, Color = "auto" },
                new InsideVerticalBorder { Val = BorderValues.Single, Size = 4, Color = "auto" }),
            new TableWidth { Width = "5000", Type = TableWidthUnitValues.Pct });
        table.Append(tblPr);

        // Header row
        if (tableEl.TryGetProperty("headers", out var headers) &&
            headers.ValueKind == System.Text.Json.JsonValueKind.Array)
        {
            var headerRow = new TableRow();
            foreach (var h in headers.EnumerateArray())
            {
                var cellText = h.GetString() ?? "";
                var cell = new TableCell(
                    new TableCellProperties(
                        new Shading { Val = ShadingPatternValues.Clear, Fill = "D9E2F3" }),
                    new Paragraph(
                        new ParagraphProperties(new Justification { Val = JustificationValues.Center }),
                        new Run(new RunProperties(new Bold()), new Text(cellText))));
                headerRow.Append(cell);
            }
            table.Append(headerRow);
        }

        // Data rows
        if (tableEl.TryGetProperty("rows", out var rows) &&
            rows.ValueKind == System.Text.Json.JsonValueKind.Array)
        {
            foreach (var row in rows.EnumerateArray())
            {
                if (row.ValueKind != System.Text.Json.JsonValueKind.Array) continue;
                var tableRow = new TableRow();
                foreach (var cellVal in row.EnumerateArray())
                {
                    var cellText = cellVal.GetString() ?? cellVal.ToString();
                    var cell = new TableCell(new Paragraph(new Run(new Text(cellText))));
                    tableRow.Append(cell);
                }
                table.Append(tableRow);
            }
        }

        body.Append(table);
        // Spacing paragraph after table
        body.Append(new Paragraph());
    }

    /// <summary>
    /// Add a list from a flat element with "items" and optional "style".
    /// <paramref name="inferredStyle"/> (from the caller's type alias, e.g. "ol" → "numbered")
    /// is used when the element itself doesn't carry an explicit "style" field.
    /// </summary>
    private static void AddList(Body body, System.Text.Json.JsonElement listEl, string? inferredStyle = null)
    {
        string style;
        if (listEl.TryGetProperty("style", out var s) && s.ValueKind == System.Text.Json.JsonValueKind.String)
            style = s.GetString() ?? "bullet";
        else
            style = inferredStyle ?? "bullet";

        if (listEl.TryGetProperty("items", out var items) &&
            items.ValueKind == System.Text.Json.JsonValueKind.Array)
        {
            AddListItems(body, items, style);
        }
    }

    /// <summary>
    /// Render list items as prefixed paragraphs (bullet or numbered).
    /// </summary>
    private static void AddListItems(Body body, System.Text.Json.JsonElement items, string style)
    {
        int index = 1;
        foreach (var item in items.EnumerateArray())
        {
            var text = item.ValueKind == System.Text.Json.JsonValueKind.String
                ? item.GetString() ?? ""
                : (item.TryGetProperty("text", out var t) ? t.GetString() ?? "" : "");
            var prefix = style == "numbered" ? $"{index}. " : "\u2022 ";
            var para = new Paragraph(
                new ParagraphProperties(
                    new Indentation { Left = "720", Hanging = "360" }),
                new Run(new Text(prefix + text) { Space = SpaceProcessingModeValues.Preserve }));
            body.Append(para);
            index++;
        }
    }
}
