# Python Script, API Version = V23

ClearAll()
DocumentOpen.Execute(r"C:\Users\user\AppData\Local\YU_Fluent\assembly_probe\coolant.step")
DocumentInsert.Execute(r"C:\Users\user\AppData\Local\YU_Fluent\assembly_probe\battery_cells.step")
options = ExportOptions.Create()
DocumentSave.Execute(
    r"C:\Users\user\AppData\Local\YU_Fluent\assembly_probe\coolant_batteries.step",
    options,
)
