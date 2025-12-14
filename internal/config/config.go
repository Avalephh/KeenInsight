package config

import (
	"fmt"
	"github.com/spf13/viper"
)

type Config struct {
	Server   Server   `mapstructure:"server"`
	Logger   Logger   `mapstructure:"logger"`
	Database Database `mapstructure:"database"`
}

type Server struct {
	Port int    `mapstructure:"port"`
	Mode string `mapstructure:"mode"`
}

type Logger struct {
	Level      string `mapstructure:"level"`
	Encoding   string `mapstructure:"encoding"`
	Filename   string `mapstructure:"filename"`
	MaxSize    int    `mapstructure:"max_size"`
	MaxAge     int    `mapstructure:"max_age"`
	MaxBackups int    `mapstructure:"max_backups"`
}

type Database struct {
	Driver       string `mapstructure:"driver"`
	Source       string `mapstructure:"source"`
	MaxIdleConns int    `mapstructure:"max_idle_conns"`
	MaxOpenConns int    `mapstructure:"max_open_conns"`
}

func Load(configPath string) (*Config, error) {
	v := viper.New()
	
	// 如果传入了路径，则使用传入的路径
	if configPath != "" {
		v.SetConfigFile(configPath)
	} else {
		// 默认查找路径
		v.AddConfigPath("configs")
		v.SetConfigName("config")
		v.SetConfigType("yaml")
	}

	// 支持环境变量覆盖，例如 RUC_SERVER_PORT=9090
	v.SetEnvPrefix("RUC")
	v.AutomaticEnv()

	if err := v.ReadInConfig(); err != nil {
		if _, ok := err.(viper.ConfigFileNotFoundError); ok {
			return nil, fmt.Errorf("config file not found")
		}
		return nil, err
	}

	var c Config
	if err := v.Unmarshal(&c); err != nil {
		return nil, err
	}

	return &c, nil
}

