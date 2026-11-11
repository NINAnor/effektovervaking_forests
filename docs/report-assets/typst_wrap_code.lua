-- Typst cannot break a long unbroken inline code span, so one in a narrow table
-- cell runs into the next column. Allow a break after "/" and "_" (zero-width space)
-- in spans longer than 24 characters. Used for the Typst/PDF output only.
function Code(el)
  if #el.text > 24 then
    el.text = el.text:gsub("([/_])", "%1\226\128\139")
    return el
  end
end
