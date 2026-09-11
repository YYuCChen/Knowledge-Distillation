# Sparkle command line driver

Unmodified source from Sparkle 2.9.6, commit
ac2def288cbff5cfc7df3ffef6abdf45b72bcb0a, sparkle-cli/.
https://github.com/sparkle-project/Sparkle/tree/ac2def288cbff5cfc7df3ffef6abdf45b72bcb0a/sparkle-cli

License: adjacent LICENSE (Sparkle MIT and embedded component notices).
Built against the matching official Sparkle.framework; compile-time direct-method
macros are disabled. The build replaces Xcode Info.plist placeholders in a temporary
copy. We invoke this driver only after explicit install consent, never with
--defer-install. It consumes a byte-identical signed feed through a loopback cache.
