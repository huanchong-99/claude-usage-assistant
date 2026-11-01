// Keep the Python UI inside a real app process. A shell launcher that forks
// python loses its bundle identity and can disappear under macOS menu managers.
// The existing interpreter and venv are reused; no runtime is copied into the app.
#import <AppKit/AppKit.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int fail(NSString *message, BOOL graphical) {
    fprintf(stderr, "AI Usage Card: %s\n", message.UTF8String);
    if (graphical) {
        [NSApplication sharedApplication];
        NSAlert *alert = [[NSAlert alloc] init];
        alert.messageText = @"AI 用量卡片启动失败";
        alert.informativeText = [message stringByAppendingString:
            @"\n请在项目目录重新运行 ./start.sh --install-app。"];
        [alert runModal];
    }
    return 1;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        NSBundle *bundle = NSBundle.mainBundle;
        NSDictionary *info = bundle.infoDictionary;
        NSString *python = info[@"AIUsagePython"];
        NSString *library = info[@"AIUsagePythonLibrary"];
        NSString *script = info[@"AIUsageScript"];
        BOOL graphical = argc == 1 && !isatty(STDERR_FILENO);
        if (![python isKindOfClass:NSString.class] ||
            ![library isKindOfClass:NSString.class] ||
            ![script isKindOfClass:NSString.class] ||
            access(python.fileSystemRepresentation, X_OK) ||
            access(library.fileSystemRepresentation, R_OK) ||
            access(script.fileSystemRepresentation, R_OK)) {
            return fail(@"找不到已配置的 Python 或项目文件。", graphical);
        }
        NSDictionary *environment = info[@"AIUsageEnvironment"];
        for (NSString *key in environment) {
            NSString *value = environment[key];
            if ([key isKindOfClass:NSString.class] && [value isKindOfClass:NSString.class])
                setenv(key.UTF8String, value.UTF8String, 0);
        }
        setenv("QUOTA_CARD_NATIVE_APP", bundle.bundlePath.fileSystemRepresentation, 1);
        if (chdir(script.stringByDeletingLastPathComponent.fileSystemRepresentation))
            return fail(@"无法打开项目目录。", graphical);

        void *handle = dlopen(library.fileSystemRepresentation, RTLD_NOW | RTLD_GLOBAL);
        if (!handle) return fail(@"无法载入 Python 运行库。", graphical);
        int (*pythonMain)(int, char **) = dlsym(handle, "Py_BytesMain");
        if (!pythonMain) return fail(@"Python 运行库不支持 Py_BytesMain。", graphical);
        char **pythonArgs = calloc((size_t)argc + 2, sizeof(char *));
        if (!pythonArgs) return fail(@"无法分配启动参数。", graphical);
        pythonArgs[0] = (char *)python.fileSystemRepresentation;
        pythonArgs[1] = (char *)script.fileSystemRepresentation;
        int count = 2;
        for (int i = 1; i < argc; ++i) {
            if (strncmp(argv[i], "-psn_", 5) != 0) pythonArgs[count++] = argv[i];
        }
        // argv[0] points to the venv interpreter, preserving sys.executable,
        // sys.prefix and its packages, while NSBundle still identifies this app.
        int result = pythonMain(count, pythonArgs);
        free(pythonArgs);
        // Python extension modules can retain symbols until process exit.
        // Do not dlclose the interpreter after Py_BytesMain finalizes it.
        return result;
    }
}
