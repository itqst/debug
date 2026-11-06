"""生成 sample_assembly.step 的脚本（需要 cadquery-ocp）：毫米单位，底板 200×100×20 mm，圆柱 Ø40×60 mm 放在 (150, 50, 20) 并绕 z 转 30°；用法 python gen_sample_step.py 输出路径"""
import sys, math
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Trsf, gp_Vec, gp_Ax1, gp_Pnt, gp_Dir
from OCP.TopLoc import TopLoc_Location
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorSurf
from OCP.TDataStd import TDataStd_Name
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs
from OCP.Interface import Interface_Static
doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
colors = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
assembly = shapes.NewShape()
TDataStd_Name.Set_s(assembly, TCollection_ExtendedString("样件装配"))
plate = shapes.AddShape(BRepPrimAPI_MakeBox(200.0, 100.0, 20.0).Shape(), False)
TDataStd_Name.Set_s(plate, TCollection_ExtendedString("底板"))
colors.SetColor(plate, Quantity_Color(0.2, 0.4, 0.9, Quantity_TOC_RGB), XCAFDoc_ColorSurf)
post = shapes.AddShape(BRepPrimAPI_MakeCylinder(20.0, 60.0).Shape(), False)
TDataStd_Name.Set_s(post, TCollection_ExtendedString("立柱"))
colors.SetColor(post, Quantity_Color(0.9, 0.5, 0.1, Quantity_TOC_RGB), XCAFDoc_ColorSurf)
shapes.AddComponent(assembly, plate, TopLoc_Location())
t = gp_Trsf(); t.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(30))
m = gp_Trsf(); m.SetTranslation(gp_Vec(150.0, 50.0, 20.0))
shapes.AddComponent(assembly, post, TopLoc_Location(m.Multiplied(t)))
shapes.UpdateAssemblies()
Interface_Static.SetCVal_s("write.step.unit", "MM")
writer = STEPCAFControl_Writer()
writer.SetNameMode(True); writer.SetColorMode(True)
writer.Transfer(doc, STEPControl_AsIs)
print(writer.Write(sys.argv[1]))
