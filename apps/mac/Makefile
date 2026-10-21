APP_NAME = PromptBridge
BUILD_DIR = build
APP_BUNDLE = $(BUILD_DIR)/$(APP_NAME).app
CONTENTS_DIR = $(APP_BUNDLE)/Contents
MACOS_DIR = $(CONTENTS_DIR)/MacOS
RESOURCES_DIR = $(CONTENTS_DIR)/Resources

SWIFT_FILES = $(wildcard Sources/*.swift)
TEST_FILES = $(wildcard Tests/*.swift)
SWIFTC = swiftc
SWIFTC_FLAGS = -O

all: $(APP_BUNDLE)

$(APP_BUNDLE): $(SWIFT_FILES) Info.plist Resources/AppIcon.icns
	@mkdir -p $(MACOS_DIR)
	@mkdir -p $(RESOURCES_DIR)
	$(SWIFTC) $(SWIFTC_FLAGS) $(SWIFT_FILES) -o $(MACOS_DIR)/$(APP_NAME)
	@cp Info.plist $(CONTENTS_DIR)/
	@cp Resources/AppIcon.icns $(RESOURCES_DIR)/
	@echo "App bundle created at $(APP_BUNDLE)"

# K-040: /Applications/PromptBridge.app (replaced as a whole; the server stays a launchd agent)
install: $(APP_BUNDLE)
	@rm -rf /Applications/$(APP_NAME).app
	@cp -R $(APP_BUNDLE) /Applications/
	@echo "Installed /Applications/$(APP_NAME).app"

clean:
	@rm -rf $(BUILD_DIR)

test:
	$(SWIFTC) Sources/Security.swift $(TEST_FILES) -o /tmp/promptbridge-mac-tests
	/tmp/promptbridge-mac-tests

run: all
	@open $(APP_BUNDLE)
