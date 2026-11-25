using System;
using System.Runtime.InteropServices;

namespace OfficialDesktop {
    public sealed class IconPixels {
        public int Width, Height;
        public byte[] Bgra;
    }

    public static class ShellIcon {
        [StructLayout(LayoutKind.Sequential)]
        private struct Size { public int Width, Height; }
        [StructLayout(LayoutKind.Sequential)]
        private struct BitmapObject {
            public int Type, Width, Height, WidthBytes;
            public short Planes, BitsPixel;
            public IntPtr Bits;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct BitmapInfo {
            public uint Size;
            public int Width, Height;
            public ushort Planes, BitCount;
            public uint Compression, ImageSize;
            public int XPelsPerMeter, YPelsPerMeter;
            public uint ClrUsed, ClrImportant, Color;
        }
        [ComImport,InterfaceType(ComInterfaceType.InterfaceIsIUnknown),Guid("BCC18B79-BA16-442F-80C4-8A59C30C463B")]
        private interface IImageFactory {
            [PreserveSig] int GetImage(Size size, uint flags, out IntPtr bitmap);
        }
        [DllImport("shell32.dll",CharSet=CharSet.Unicode,ExactSpelling=true)]
        private static extern int SHCreateItemFromParsingName(string path, IntPtr bind, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IImageFactory item);
        [DllImport("gdi32.dll",EntryPoint="GetObjectW")] private static extern int GetObject(IntPtr bitmap,int size,out BitmapObject obj);
        [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr bitmap);
        [DllImport("user32.dll")] private static extern IntPtr GetDC(IntPtr window);
        [DllImport("user32.dll")] private static extern int ReleaseDC(IntPtr window,IntPtr dc);
        [DllImport("gdi32.dll")] private static extern int GetDIBits(IntPtr dc,IntPtr bitmap,uint first,uint count,[Out] byte[] pixels,ref BitmapInfo info,uint usage);

        public static IconPixels Read(string registeredItem) {
            Guid iid=typeof(IImageFactory).GUID;
            IImageFactory item;
            Marshal.ThrowExceptionForHR(SHCreateItemFromParsingName(registeredItem,IntPtr.Zero,ref iid,out item));
            IntPtr bitmap=IntPtr.Zero;
            try {
                // SIIGBF_ICONONLY requests the registered icon even if the shell can produce thumbnails.
                Marshal.ThrowExceptionForHR(item.GetImage(new Size { Width=128, Height=128 },4,out bitmap));
                BitmapObject obj;
                if (bitmap==IntPtr.Zero || GetObject(bitmap,Marshal.SizeOf(typeof(BitmapObject)),out obj)==0 ||
                    obj.Width<1 || obj.Height<1 || obj.Width>1024 || obj.Height>1024) throw new InvalidOperationException("No usable application icon.");
                var result=new IconPixels { Width=obj.Width, Height=obj.Height, Bgra=new byte[obj.Width*obj.Height*4] };
                var info=new BitmapInfo { Size=40,Width=obj.Width,Height=-obj.Height,Planes=1,BitCount=32 };
                IntPtr dc=GetDC(IntPtr.Zero);
                if (dc==IntPtr.Zero) throw new InvalidOperationException("Icon device context unavailable.");
                try {
                    if (GetDIBits(dc,bitmap,0,(uint)obj.Height,result.Bgra,ref info,0)!=obj.Height)
                        throw new InvalidOperationException("Cannot read the registered icon.");
                } finally { ReleaseDC(IntPtr.Zero,dc); }
                bool alpha=false,rgb=false;
                for (int i=0;i<result.Bgra.Length;i+=4) {
                    alpha|=result.Bgra[i+3]!=0;
                    rgb|=(result.Bgra[i]|result.Bgra[i+1]|result.Bgra[i+2])!=0;
                }
                if (!alpha && !rgb) throw new InvalidOperationException("Empty application icon.");
                // Legacy DDBs carry BGRX. Shell icons with an alpha channel retain transparency.
                if (!alpha) for (int i=3;i<result.Bgra.Length;i+=4) result.Bgra[i]=255;
                return result;
            } finally {
                if (bitmap!=IntPtr.Zero) DeleteObject(bitmap);
                Marshal.ReleaseComObject(item);
            }
        }
    }
}
