package response

import (
	"net/http"

	"github.com/gin-gonic/gin"
)

type Response struct {
	Code int         `json:"code"`
	Msg  string      `json:"msg"`
	Data interface{} `json:"data"`
}

const (
	CodeSuccess = 0
	CodeError   = 1
)

func Result(c *gin.Context, httpStatus int, code int, msg string, data interface{}) {
	c.JSON(httpStatus, Response{
		Code: code,
		Msg:  msg,
		Data: data,
	})
}

func Success(c *gin.Context, data interface{}) {
	Result(c, http.StatusOK, CodeSuccess, "success", data)
}

func Error(c *gin.Context, code int, msg string) {
	Result(c, http.StatusOK, code, msg, nil)
}

func ServerError(c *gin.Context, err error) {
	msg := "internal server error"
	if err != nil {
		msg = err.Error()
	}
	Result(c, http.StatusInternalServerError, CodeError, msg, nil)
}

